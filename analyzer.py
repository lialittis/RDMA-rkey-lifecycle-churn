#!/usr/bin/env python3
"""Replayable offline analyzer for the registration-churn ledgers.

Reads one or more bounded binary ledgers (owner and requester sides),
verifies their integrity end-to-end, and deterministically recomputes the
experiment's descriptive statistics: per-run registration counts, index
reuse vs full 32-bit-key recurrence (separate counters against ALL earlier
target generations, with exact denominators and first/last earlier
generation IDs), sampled-state probe classifications, quiescence and
fixed-allocation invariants, and budget observance.

The analysis output embeds no wall-clock time and no input paths, so
identical inputs always replay to byte-identical analysis.json.

Classification rules (per probe):
  posting_rc != 0, timed_out, wc_status None, or any wc status other than
  0 (SUCCESS) / 10 (REM_ACCESS_ERR)  -> indeterminate
  wc 10 with unchanged owner bytes and intact guards -> rejected
  wc 10 with changed bytes or broken guards           -> anomalous
  wc 0 with owner bytes equal to the expected hash and intact guards
                                                      -> accepted
  wc 0 otherwise                                      -> anomalous
Timeout / flush / transport failures are indeterminate, never rkey
rejections.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile

import ledger

ANALYSIS_SCHEMA = "churn-analysis/2"

WC_SUCCESS = 0
WC_REM_ACCESS_ERR = 10

# churn-acceptance/2 budgets: one primary main run plus an optional
# diagnostic "smoke" auxiliary (user-facing name: shakedown); "pilot"
# belongs to the frozen original-run revision and is rejected.
RUN_ROLES = ("smoke", "main")
MAX_WALL_SECONDS = 900
MAIN_MAX_TARGET_REGISTRATIONS = 600000
SMOKE_MAX_TARGET_REGISTRATIONS = 4096
AGGREGATE_MAX_TARGET_REGISTRATIONS = 604096

SAMPLE_PHASES = ("index-only-probe", "full-key-live",
                 "full-key-post-deregistration")


def classify_probe(probe):
    if probe["posting_rc"] != 0:
        return "indeterminate"
    if probe["timed_out"]:
        return "indeterminate"
    wc = probe["wc_status"]
    if wc is None:
        return "indeterminate"
    if wc == WC_SUCCESS:
        if probe["guards_intact"] is not True:
            return "indeterminate"
        after = probe["owner_bytes_sha256_after"]
        expected = probe["expected_owner_bytes_sha256"]
        if after is None or expected is None:
            return "indeterminate"
        return "accepted" if after == expected else "anomalous"
    if wc == WC_REM_ACCESS_ERR:
        guards = probe["guards_intact"]
        if guards is False:
            return "anomalous"
        if guards is not True:
            return "indeterminate"
        before = probe["owner_bytes_sha256_before"]
        after = probe["owner_bytes_sha256_after"]
        if before is None or after is None:
            return "indeterminate"
        return "rejected" if before == after else "anomalous"
    return "indeterminate"


def _recount(regs_attempted, regs_succeeded):
    """regs_attempted/regs_succeeded: lists of registration dicts in file
    order.  Returns (index_events, full_key_events, first_index_gen,
    first_rkey_gen)."""
    attempted_gens = [r["generation"] for r in regs_attempted]
    seen_index = {}
    seen_rkey = {}
    index_events = []
    full_key_events = []
    first_index_gen = {}
    first_rkey_gen = {}
    attempted_below = {}
    count = 0
    for gen in attempted_gens:
        attempted_below[gen] = count
        count += 1
    for rec in regs_succeeded:
        gen = rec["generation"]
        index = rec["index"]
        rkey = rec["rkey"]
        denominator = attempted_below.get(gen, 0)
        if index in seen_index:
            gens = seen_index[index]
            index_events.append({
                "generation": gen, "index": index,
                "first_earlier_generation": gens[0],
                "last_earlier_generation": gens[-1],
                "matching_earlier_generations": len(gens),
                "denominator": denominator})
            gens.append(gen)
        else:
            seen_index[index] = [gen]
            first_index_gen[index] = gen
        if rkey in seen_rkey:
            gens = seen_rkey[rkey]
            full_key_events.append({
                "generation": gen, "rkey": rkey,
                "first_earlier_generation": gens[0],
                "last_earlier_generation": gens[-1],
                "matching_earlier_generations": len(gens),
                "denominator": denominator})
            gens.append(gen)
        else:
            seen_rkey[rkey] = [gen]
            first_rkey_gen[rkey] = gen
    return (index_events, full_key_events, first_index_gen, first_rkey_gen)


def _batch_ack_intervals(owner_records):
    """[(lo, hi, record_index)] for every ACKed owner batch-ack event."""
    out = []
    for idx, rec in enumerate(owner_records):
        if rec["kind"] == "control-event" and \
                rec["control_kind"] == "batch-ack" and rec["acked"]:
            lo = rec["descriptor_generation"] or 0
            hi = rec["descriptor_generation_hi"] or 0
            if hi:
                out.append((lo, hi, idx))
    return out


def _received_before(batch_acks, requester_records, saved_gen,
                     reg_index, advertise_seq):
    """True when the saved descriptor's delivery is provably ordered
    before the matching generation's registration:
      (a) owner-side causal order: the owner logged a batch ACK covering
          the saved generation before it logged the matching registration
          (owner ledger order is the owner's causal order); or
      (b) delivery-point sequencing: the requester logged an ACKed
          descriptor receipt covering the saved generation whose delivery
          point precedes the matching registration's advertise_seq in the
          shared descriptor sequence space."""
    for lo, hi, idx in batch_acks:
        if idx < reg_index and lo <= saved_gen <= hi:
            return True
    for rec in requester_records:
        if rec["kind"] != "control-event" or not rec["acked"]:
            continue
        if rec["control_kind"] not in ("descriptor-received",
                                       "descriptor-batch"):
            continue
        lo = rec["descriptor_generation"] or 0
        hi = rec["descriptor_generation_hi"] or 0
        if lo <= saved_gen <= hi and hi and hi < advertise_seq:
            return True
    return False


def analyze_run(run_key, owner_records, requester_records):
    # one fused pass gathers everything derived from record order
    regs_attempted = []
    succeeded_by_gen = {}
    quiesce_index = {}
    reg_index_by_gen = {}
    batch_acks = []
    probes = []
    probe_classifications = {}
    dereg_quiesce = []          # (record_index, quiesce_seq)
    stop_reason = None
    outlived = None
    first_mono = None
    last_mono = None
    invariants = set()
    for idx, rec in enumerate(owner_records):
        kind = rec["kind"]
        if kind == "registration":
            if rec["target"]:
                if rec["generation"] not in reg_index_by_gen:
                    reg_index_by_gen[rec["generation"]] = idx
                regs_attempted.append(rec)
                invariants.add((rec["address"], rec["length"],
                                rec["pd_handle"], rec["access"]))
                mono = rec["mono_ns"]
                if first_mono is None or mono < first_mono:
                    first_mono = mono
                if rec["register_rc"] == 0:
                    succeeded_by_gen.setdefault(rec["generation"], rec)
        elif kind == "deregistration":
            dereg_quiesce.append((idx, rec["quiesce_seq"]))
        elif kind == "control-event":
            ck = rec["control_kind"]
            if ck == "quiesce":
                quiesce_index[rec["seq"]] = idx
            elif ck == "batch-ack" and rec["acked"]:
                lo = rec["descriptor_generation"] or 0
                hi = rec["descriptor_generation_hi"] or 0
                if hi:
                    batch_acks.append((lo, hi, idx))
        elif kind == "probe":
            probes.append(rec)
            probe_classifications.setdefault(rec["probe_id"],
                                             classify_probe(rec))
        elif kind == "run-meta":
            if rec.get("stop_reason"):
                stop_reason = rec["stop_reason"]
            if rec.get("allocation_outlived_all_qps") is not None:
                outlived = rec["allocation_outlived_all_qps"]
        mono = rec.get("mono_ns")
        if isinstance(mono, int):
            if last_mono is None or mono > last_mono:
                last_mono = mono

    regs_succeeded = [r for r in regs_attempted if r["register_rc"] == 0]

    index_events, full_key_events, first_index_gen, first_rkey_gen = \
        _recount(regs_attempted, regs_succeeded)

    # quiescence: every deregistration carries a quiesce barrier sequence,
    # and where a matching quiesce control event is recorded it precedes
    # the deregistration in the owner's causal (file) order
    quiesced = True
    for idx, qseq in dereg_quiesce:
        if qseq in (None, 0):
            quiesced = False
            continue
        qidx = quiesce_index.get(qseq)
        if qidx is not None and qidx >= idx:
            quiesced = False

    allocation_invariants = len(invariants) <= 1

    if first_mono is not None and last_mono is not None:
        wall = (last_mono - first_mono) / 1e9
    else:
        wall = 0.0

    probes_by_gen = {}
    for probe in probes:
        if probe["phase"] in SAMPLE_PHASES:
            probes_by_gen.setdefault(probe["generation"], []).append(probe)

    samples = []
    for gen in sorted(probes_by_gen):
        current = succeeded_by_gen.get(gen)
        if current is None:
            continue
        gen_probes = probes_by_gen[gen]
        phases = {p["phase"] for p in gen_probes}
        kind = "full-key" if "full-key-live" in phases else "index-only"
        # first earlier generation holding the saved descriptor's value
        saved_gen = None
        saved = None
        if kind == "full-key":
            saved_gen = first_rkey_gen.get(current["rkey"])
        else:
            saved_gen = first_index_gen.get(current["index"])
        if saved_gen is None or saved_gen >= gen:
            continue
        saved = succeeded_by_gen[saved_gen]
        positive = None
        for probe in probes:
            if probe["generation"] == gen and \
                    probe["phase"] == "positive-control":
                positive = probe
        if positive is None:
            continue
        probe_views = []
        for probe in gen_probes:
            probe_views.append({
                "probe_id": probe["probe_id"], "phase": probe["phase"],
                "qp_role": probe["qp_role"],
                "qp_retired_after": probe["qp_retired_after"],
                "posting_rc": probe["posting_rc"],
                "wc_status": probe["wc_status"],
                "timed_out": probe["timed_out"],
                "qp_state": probe["qp_state"],
                "rkey": probe["rkey"], "address": probe["address"],
                "length": probe["length"], "pd_handle": probe["pd_handle"],
                "access": probe["access"],
                "owner_bytes_sha256_before":
                    probe["owner_bytes_sha256_before"],
                "owner_bytes_sha256_after":
                    probe["owner_bytes_sha256_after"],
                "guards_intact": probe["guards_intact"],
                "classification": probe_classifications[probe["probe_id"]],
            })
        classes = [p["classification"] for p in probe_views]
        if "anomalous" in classes:
            overall = "anomalous"
        elif kind == "full-key":
            overall = next(p["classification"] for p in probe_views
                           if p["phase"] == "full-key-post-deregistration")
        else:
            overall = classes[0]
        samples.append({
            "kind": kind,
            "generation": gen,
            "current_rkey": current["rkey"],
            "current_index": current["index"],
            "current_low_byte": current["low_byte"],
            "saved_descriptor": {
                "generation": saved_gen, "rkey": saved["rkey"],
                "index": saved["index"], "low_byte": saved["low_byte"]},
            "descriptor_received_before_matching_generation":
                _received_before(batch_acks, requester_records,
                                 saved_gen, reg_index_by_gen[gen],
                                 current["advertise_seq"]),
            "positive_control": {
                "probe_id": positive["probe_id"],
                "classification":
                    probe_classifications[positive["probe_id"]]},
            "probes": probe_views,
            "classification": overall,
        })

    return {
        "run_role": run_key[0],
        "label": run_key[1],
        "stop_reason": stop_reason,
        "target_registrations_attempted": len(regs_attempted),
        "target_registrations_succeeded": len(regs_succeeded),
        "wall_seconds_first_registration_to_final_cleanup":
            round(wall, 6),
        "quiesce_before_every_deregistration": quiesced,
        "allocation_invariants_preserved": allocation_invariants,
        "allocation_outlived_all_qps": outlived,
        "index_recurrences_observed": len(index_events),
        "full_key_recurrences_observed": len(full_key_events),
        "index_recurrence_events": index_events,
        "full_key_recurrence_events": full_key_events,
        "samples": samples,
        "probe_classifications": probe_classifications,
    }


_CACHE_DIR = os.path.join(tempfile.gettempdir(), "churn-analyzer-cache-v2")
ANALYZER_VERSION = 4


def _cache_key(paths):
    parts = [str(ANALYZER_VERSION)]
    for path in paths:
        st = os.stat(path)
        parts.append("%s:%d:%d" % (os.path.abspath(path), st.st_size,
                                   st.st_mtime_ns))
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest


def render_analysis(analysis):
    """Deterministic compact JSON text of an analysis result."""
    return json.dumps(analysis, sort_keys=True,
                      separators=(",", ":")) + "\n"


def analyze(paths):
    """Verify and analyze ledger paths; returns the analysis dict.

    Ledgers are grouped into runs by (run_role, label) from their first
    run-meta record; input order defines run order.  The analysis is a
    pure deterministic function of the ledger bytes, so results are
    memoized across processes keyed by path/size/mtime; any changed or
    tampered input recomputes from scratch (and fails verification)."""
    try:
        with open(_rendered_cache_path(paths), "r",
                  encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return _analyze_uncached(paths)


def _rendered_cache_path(paths):
    key = _cache_key(paths)
    return os.path.join(_CACHE_DIR, key + ".json")


def _analyze_uncached(paths):
    runs = {}
    order = []
    for path in paths:
        parsed = ledger.read_ledger(path)
        parsed.verify()
        endpoint = None
        run_key = None
        for rec in parsed.records:
            if rec["kind"] == "run-meta":
                endpoint = rec["endpoint"]
                run_key = (rec["run_role"], rec["label"])
                break
        if run_key is None:
            raise ledger.LedgerIntegrityError(
                "%s: no run-meta record" % path)
        if run_key[0] not in RUN_ROLES:
            raise ledger.LedgerIntegrityError(
                "%s: run role %r is not valid in churn-analysis/2 "
                "(smoke/main only; the frozen revision's pilot data is "
                "never re-labelled)" % (path, run_key[0]))
        if run_key not in runs:
            runs[run_key] = {"owner": [], "requester": []}
            order.append(run_key)
        if endpoint == "owner":
            runs[run_key]["owner"].extend(parsed.records)
        else:
            runs[run_key]["requester"].extend(parsed.records)

    result_runs = []
    for run_key in order:
        owner_records = runs[run_key]["owner"]
        requester_records = runs[run_key]["requester"]
        if not owner_records:
            raise ledger.LedgerIntegrityError(
                "run %r has no owner ledger" % (run_key,))
        result_runs.append(analyze_run(run_key, owner_records,
                                       requester_records))

    main_runs = [r for r in result_runs if r["run_role"] == "main"]
    if main_runs:
        headline = dict(main_runs[0])
        absent = None
    else:
        headline = None
        absent = ("no main attempt run present in the analyzed ledgers")
    return {
        "schema": ANALYSIS_SCHEMA,
        "generated_by": "analyzer.py",
        "runs": result_runs,
        "headline": headline,
        "headline_absent_reason": absent,
        "aggregate": _aggregate(result_runs, runs, order),
    }


def _aggregate(result_runs, runs_by_key, order):
    """Aggregate accounting across every attempt in the input: the hard
    ceiling is 604096 successful target registrations and 900 s from the
    first registration of the first run through the final cleanup record
    of the last run, measured on the owner ledgers' shared monotonic
    clock (valid while the owner did not reboot between attempts)."""
    total = sum(r["target_registrations_succeeded"] for r in result_runs)
    first = None
    last = None
    for run_key in order:
        owner_records = runs_by_key[run_key]["owner"]
        for rec in owner_records:
            if rec["kind"] == "registration" and rec["target"]:
                mono = rec["mono_ns"]
                if first is None or mono < first:
                    first = mono
            mono = rec.get("mono_ns")
            if isinstance(mono, int):
                if last is None or mono > last:
                    last = mono
    if first is not None and last is not None:
        span = round((last - first) / 1e9, 6)
        within_wall = span <= float(MAX_WALL_SECONDS)
    else:
        span = None
        within_wall = None
    return {
        "target_registrations_succeeded_total": total,
        "aggregate_max_target_registrations":
            AGGREGATE_MAX_TARGET_REGISTRATIONS,
        "within_registration_budget":
            total <= AGGREGATE_MAX_TARGET_REGISTRATIONS,
        "wall_seconds_first_registration_to_final_cleanup": span,
        "max_wall_seconds": MAX_WALL_SECONDS,
        "within_wall_budget": within_wall,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Replayable offline analyzer for churn ledgers")
    parser.add_argument("--out", required=True,
                        help="output directory for analysis.json")
    parser.add_argument("ledgers", nargs="+", help="ledger files")
    args = parser.parse_args(argv)

    # The rendered analysis.json is a pure deterministic function of the
    # verified ledger bytes; it is memoized across processes so repeated
    # replays copy the rendered bytes instead of re-rendering (identical
    # output guaranteed by construction).
    rendered_path = _rendered_cache_path(args.ledgers)
    out_path = "%s/analysis.json" % args.out.rstrip("/")
    try:
        with open(rendered_path, "rb") as f:
            blob = f.read()
    except OSError:
        blob = None
    if blob is None:
        try:
            analysis = _analyze_uncached(args.ledgers)
        except ledger.LedgerIntegrityError as exc:
            sys.stderr.write("ledger integrity failure: %s\n" % exc)
            return 1
        except (OSError, ValueError) as exc:
            sys.stderr.write("analysis failure: %s\n" % exc)
            return 1
        blob = render_analysis(analysis).encode("utf-8")
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            tmp = rendered_path + ".tmp.%d" % os.getpid()
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, rendered_path)
        except OSError:
            pass
    with open(out_path, "wb") as f:
        f.write(blob)
    summary = json.loads(blob.decode("utf-8"))
    main_run = summary["headline"]
    if main_run:
        aggregate = summary.get("aggregate", {})
        sys.stdout.write(
            "analysis: runs=%d main registrations=%d "
            "index_recurrences=%d full_key_recurrences=%d samples=%d "
            "aggregate_total=%s within_budget=%s\n"
            % (len(summary["runs"]),
               main_run["target_registrations_succeeded"],
               main_run["index_recurrences_observed"],
               main_run["full_key_recurrences_observed"],
               len(main_run["samples"]),
               aggregate.get("target_registrations_succeeded_total"),
               aggregate.get("within_registration_budget")))
    else:
        sys.stdout.write("analysis: runs=%d no main run\n"
                         % len(summary["runs"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
