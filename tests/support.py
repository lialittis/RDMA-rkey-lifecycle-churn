"""Shared support code for the churn-experiment acceptance tests.

This module also pins, in one place, the acceptance CONTRACT that the
implementation under this repository must satisfy.  The tests are the
specification; the implementation (verify.py, ledger.py, analyzer.py,
run-manifest.json, results/) must be built to pass them.

Contract summary (churn-acceptance/1)
=====================================

Repository layout (all paths relative to the repository root):

- ``run-manifest.json``  -- the experiment manifest (see MANIFEST SCHEMA).
- ``verify.py``          -- ``python3 verify.py [--strict] MANIFEST`` exits 0
  only when the manifest is structurally valid, its pinned config/budget/
  prohibition/sample-plan values match this contract, every listed artifact
  exists with matching size and SHA-256, every required artifact role is
  present (global and per-run), and every run has a valid stop reason (with
  a preserved failure explanation for early stops).  Any violation exits
  nonzero.  Manifest artifact paths are relative to the manifest's own
  directory.
- ``ledger.py``          -- the bounded binary ledger module.  API:
  ``MAGIC`` (bytes), ``VERSION`` (int), ``LedgerIntegrityError(Exception)``,
  ``read_ledger(path) -> Ledger`` where ``Ledger.records`` is a list of dict
  records and ``Ledger.verify()`` validates the whole file (raising
  ``LedgerIntegrityError`` on any corruption, truncation or checksum
  mismatch), and ``LedgerWriter(path)`` usable as a context manager with
  ``.append(record_dict)``, ``.checkpoint()`` and ``.close()``.  The ledger
  is a fixed-layout binary format (not per-registration JSONL) with a
  documented schema (artifact role ``ledger-schema``) covering magic,
  version, record layout, checksum algorithm and checkpoint interval.
- ``analyzer.py``        -- replayable offline analyzer.  Importable as
  ``analyze(ledger_paths) -> dict`` and runnable as
  ``python3 analyzer.py --out DIR LEDGER [LEDGER ...]`` which writes
  ``DIR/analysis.json`` and exits 0 (nonzero on unreadable/corrupt input).
  Analysis output is deterministic: identical inputs replayed twice give
  byte-identical analysis.json (no embedded wall-clock time).
- ``results/``           -- archived experiment outputs: English report,
  analysis, per-run raw logs, ledgers, diagnostics, configurations, stops.

Manifest schema (``schema`` == "churn-manifest/1"):

- ``experiment``: ``owner_endpoint`` "192.168.122.112",
  ``requester_endpoint`` "192.168.122.111", ``remote_directory``
  "/tmp/rkey-lifecycle-churn-glm".
- ``config``: ``allocation_bytes`` 4096; ``probe`` = nonzero 64-byte RC
  RDMA WRITE at offset 64; ``mr_access`` includes LOCAL_WRITE and
  REMOTE_WRITE; ``fixed_allocation``, ``allocation_alive_until_qps_destroyed``
  and ``quiesce_before_deregistration`` all true.
- ``budgets``: ``max_wall_seconds`` 900, ``max_target_registrations``
  1048576, ``calibration_prefix_registrations`` 4096.
- ``prohibitions``: includes device-reset, index-range-restriction,
  key-injection, failed-probe-retry, synthetic-recurrence-as-observation.
- ``sample_plan``: ``index_only`` 4, ``full_key`` 8,
  ``positive_stopping_rule`` false.
- ``runs``: exactly one ``{"role": "pilot", ...}`` and zero or one main run;
  each entry has non-empty ``label`` and ``stop_reason`` from STOP_REASONS.
  A run whose stop_reason is in STOP_REASONS_REQUIRING_EXPLANATION must have
  a per-run ``failure-explanation`` artifact.  If no main run exists, a
  global ``main-attempt-explanation`` artifact must.
- ``artifacts``: list of ``{"role", "path", "sha256", "bytes"}`` plus
  optional ``"run"`` ("pilot"/"main") for run-scoped artifacts.  Paths are
  relative, no ``..``, unique, sha256 is 64 lowercase hex, bytes matches the
  real file size.

Required artifact roles:

- global: harness-source (at least one, at least one ending .c), build-log,
  binary (>=1), environment-owner, environment-requester, remote-commands,
  ledger-schema, report, analysis, negative-checks.
- per run (pilot and, when present, main): owner-ledger, requester-ledger,
  raw-logs (>=1), diagnostics (>=1), run-config, stop-record.

Ledger record kinds and required fields (extra fields allowed):

- ``run-meta``: run_role, label, endpoint; stop_reason and
  allocation_outlived_all_qps when the run closed cleanly.
- ``registration``: generation (contiguous 1..N over target attempts),
  target (bool; only target registrations are counted), rkey (full 32-bit),
  index (== rkey >> 8), low_byte (== rkey & 0xFF), address, length (4096),
  pd_handle, access, register_rc (0 == success), advertise_seq
  (control-channel sequence of this generation's descriptor advertisement),
  mono_ns.  Address/length/pd/access are constant across all target
  registrations of a run (fixed allocation).
- ``deregistration``: generation, deregister_rc, quiesce_seq (non-null:
  the control sequence of the quiesce barrier preceding this dereg),
  mono_ns.
- ``control-event``: seq (strictly increasing), control_kind, acked,
  descriptor_generation (for descriptor deliveries), mono_ns.
- ``probe``: probe_id, generation, phase (positive-control |
  index-only-probe | full-key-live | full-key-post-deregistration), side,
  posting_rc, wc_status, timed_out, qp_role (fresh | original | control),
  qp_state, qp_retired_after, rkey, address, length, pd_handle, access,
  owner_bytes_sha256_before, owner_bytes_sha256_after,
  expected_owner_bytes_sha256, guards_intact, mono_ns.
- ``checkpoint``: periodic (schema doc states the interval).

Analyzer classification rules (deterministic, per probe):

- posting_rc != 0, or timed_out, or wc_status in {3 transport, 5 flush,
  8 retry-exceed} or any status other than 0/10 -> ``indeterminate``.
- wc_status == 10 (remote access error) with owner bytes unchanged and
  guards intact -> ``rejected``; with changed bytes or broken guards ->
  ``anomalous``.
- wc_status == 0 with owner_bytes_sha256_after == expected hash and guards
  intact -> ``accepted``; otherwise ``anomalous``.
Timeout/flush/transport outcomes are indeterminate, never rkey rejections.

Analyzer output (``analysis.json``, schema "churn-analysis/1"): a
``runs`` list (one object per run discovered from ledger run-meta records,
input order) and ``headline`` (a copy of the main run object, or null plus
``headline_absent_reason``).  Each run object carries:
target_registrations_attempted, target_registrations_succeeded (<= 1048576),
calibration_prefix_registrations (<= 4096, part of the succeeded count),
wall_seconds_first_registration_to_final_cleanup (<= 900),
quiesce_before_every_deregistration (true),
allocation_invariants_preserved (true),
allocation_outlived_all_qps (bool or null),
index_recurrences_observed / full_key_recurrences_observed (separate
counters against ALL earlier target generations; full-key events are a
subset of index events), ``index_recurrence_events`` /
``full_key_recurrence_events`` lists where each event records generation,
matching value (index or rkey), first_earlier_generation,
last_earlier_generation, matching_earlier_generations (count) and the exact
denominator (number of earlier target generations), ``samples`` (capped at
4 index-only + 8 full-key; each sample has kind, generation,
current_rkey/current_index/current_low_byte, saved_descriptor {generation,
rkey, index, low_byte}, descriptor_received_before_matching_generation
(bool), positive_control {probe_id, classification} which must be
"accepted", probes with the separated raw fields plus per-probe
classification, and an overall classification), and ``probe_classifications``
(probe_id -> classification for every probe in the inputs).

Index-only samples: the saved descriptor has the same index but a DIFFERENT
rkey than the current registration; one probe through a fresh QP with
qp_retired_after true.  Full-key samples: the saved descriptor is
bit-identical (same rkey); a live probe on an unfailed QP while the
matching MR is live, then a post-deregistration probe of the same values on
a separate fresh QP.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
for _p in (TESTS_DIR, REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- pinned experiment constants (from the task's hard requirements) ------

OWNER_ENDPOINT = "192.168.122.112"          # rdma-server
REQUESTER_ENDPOINT = "192.168.122.111"      # rdma-client
REMOTE_DIRECTORY = "/tmp/rkey-lifecycle-churn-glm"

MANIFEST_PATH = os.path.join(REPO_ROOT, "run-manifest.json")
MANIFEST_SCHEMA = "churn-manifest/1"

ALLOCATION_BYTES = 4096
PROBE_SPEC = {"opcode": "RDMA_WRITE", "transport": "RC",
              "length": 64, "offset": 64, "nonzero": True}
REQUIRED_ACCESS_FLAGS = {"LOCAL_WRITE", "REMOTE_WRITE"}

BUDGETS = {"max_wall_seconds": 900,
           "max_target_registrations": 1048576,
           "calibration_prefix_registrations": 4096}

REQUIRED_PROHIBITIONS = {"device-reset", "index-range-restriction",
                         "key-injection", "failed-probe-retry",
                         "synthetic-recurrence-as-observation"}

SAMPLE_PLAN = {"index_only": 4, "full_key": 8,
               "positive_stopping_rule": False}

STOP_REASONS = {"time-budget", "count-budget", "control-failure",
                "cleanup-warning", "memory-mismatch", "probe-timeout",
                "explicit-stop"}
STOP_REASONS_REQUIRING_EXPLANATION = {"control-failure", "cleanup-warning",
                                      "memory-mismatch", "probe-timeout"}

REQUIRED_GLOBAL_ROLES = ["harness-source", "build-log", "binary",
                         "environment-owner", "environment-requester",
                         "remote-commands", "ledger-schema", "report",
                         "analysis", "negative-checks"]
REQUIRED_RUN_ROLES = ["owner-ledger", "requester-ledger", "raw-logs",
                      "diagnostics", "run-config", "stop-record"]

MAX_LEDGER_BYTES = 256 * 1024 * 1024

ANALYSIS_SCHEMA = "churn-analysis/1"

# ibverbs completion status values used by the classification rules.
WC_SUCCESS = 0
WC_TRANSPORT_ERR = 3
WC_WR_FLUSH_ERR = 5
WC_RETRY_EXC_ERR = 8
WC_REM_ACCESS_ERR = 10
INDETERMINATE_WC = {WC_TRANSPORT_ERR, WC_WR_FLUSH_ERR, WC_RETRY_EXC_ERR}
CLASSIFICATIONS = {"accepted", "rejected", "indeterminate", "anomalous"}

# Required fields per ledger record kind (keys must exist; values may be
# null only where the contract says so).
REGISTRATION_FIELDS = {"kind", "generation", "target", "rkey", "index",
                       "low_byte", "address", "length", "pd_handle",
                       "access", "register_rc", "advertise_seq", "mono_ns"}
DEREGISTRATION_FIELDS = {"kind", "generation", "deregister_rc",
                         "quiesce_seq", "mono_ns"}
CONTROL_EVENT_FIELDS = {"kind", "seq", "control_kind", "acked", "mono_ns"}
PROBE_FIELDS = {"kind", "probe_id", "generation", "phase", "posting_rc",
                "wc_status", "timed_out", "qp_role", "qp_state",
                "qp_retired_after", "rkey", "address", "length", "pd_handle",
                "access", "owner_bytes_sha256_before",
                "owner_bytes_sha256_after", "expected_owner_bytes_sha256",
                "guards_intact", "mono_ns"}

LEDGER_SCHEMA_DOC_KEYWORDS = ["magic", "checksum", "checkpoint",
                              "generation", "rkey", "index", "low byte",
                              "register", "deregister", "timestamp"]


# --- manifest helpers ------------------------------------------------------

def load_manifest():
    """Parse run-manifest.json (raises if absent: tests must fail loudly)."""
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def manifest_artifacts(manifest):
    arts = manifest.get("artifacts")
    if not isinstance(arts, list):
        raise AssertionError("manifest 'artifacts' must be a list")
    return arts


def artifacts_by_role(manifest, role, run=None):
    out = []
    for a in manifest_artifacts(manifest):
        if a.get("role") == role and a.get("run", None) == run:
            out.append(a)
    return out


def all_roles_present(manifest):
    return {a.get("role") for a in manifest_artifacts(manifest)}


def run_entries(manifest):
    runs = manifest.get("runs")
    if not isinstance(runs, list):
        raise AssertionError("manifest 'runs' must be a list")
    return runs


def artifact_fs_path(manifest_dir, artifact):
    return os.path.join(manifest_dir, artifact["path"])


def run_ledger_paths(manifest, manifest_dir):
    """[(run_entry, owner_ledger_path, requester_ledger_path), ...]."""
    out = []
    for run in run_entries(manifest):
        owner = artifacts_by_role(manifest, "owner-ledger", run=run["role"])
        requester = artifacts_by_role(manifest, "requester-ledger",
                                      run=run["role"])
        if len(owner) != 1 or len(requester) != 1:
            raise AssertionError(
                "run %r must have exactly one owner-ledger and one "
                "requester-ledger artifact" % run.get("role"))
        out.append((run, artifact_fs_path(manifest_dir, owner[0]),
                    artifact_fs_path(manifest_dir, requester[0])))
    return out


def all_ledger_paths_in_order(manifest, manifest_dir):
    """Owner+requester ledger paths in manifest run order (analyzer input)."""
    paths = []
    for _run, owner, requester in run_ledger_paths(manifest, manifest_dir):
        paths.append(owner)
        paths.append(requester)
    return paths


# --- small utilities -------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_sha256_hex(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


# --- synthetic fixtures ----------------------------------------------------
# These fixtures do NOT observe anything: they exercise the analyzer and
# verifier logic against fabricated, internally consistent data.  They are
# never counted as experimental observations.

SYNTH_ADDRESS = 0x55550000
SYNTH_LENGTH = ALLOCATION_BYTES
SYNTH_PD = 0x7f000000
SYNTH_ACCESS = "LOCAL_WRITE|REMOTE_WRITE"
SYNTH_H0 = "aa" * 16   # sentinel-filled allocation
SYNTH_H1 = "bb" * 16   # accepted WRITE payload for gen1/gen3 controls
SYNTH_H2 = "cc" * 16   # accepted WRITE payload for gen4 controls
SYNTH_H3 = "dd" * 16   # classification-fixture payload

# Fixed key plan: index reuse at gen3 (novel key), full-key reuse at gen4.
SYNTH_KEYS = {1: 0x00000111, 2: 0x00000222, 3: 0x00000133, 4: 0x00000222}


def _registration(gen, mono, target=True):
    rkey = SYNTH_KEYS[gen]
    return {"kind": "registration", "generation": gen, "target": target,
            "rkey": rkey, "index": rkey >> 8, "low_byte": rkey & 0xFF,
            "address": SYNTH_ADDRESS, "length": SYNTH_LENGTH,
            "pd_handle": SYNTH_PD, "access": SYNTH_ACCESS,
            "register_rc": 0, "advertise_seq": 10 * gen, "mono_ns": mono}


def _deregistration(gen, mono):
    return {"kind": "deregistration", "generation": gen,
            "deregister_rc": 0, "quiesce_seq": 10 * gen + 5, "mono_ns": mono}


def _probe(probe_id, gen, phase, mono, **overrides):
    rec = {"kind": "probe", "probe_id": probe_id, "generation": gen,
           "phase": phase, "side": "owner", "posting_rc": 0,
           "wc_status": WC_SUCCESS, "timed_out": False,
           "qp_role": "control", "qp_state": 3,  # RTS
           "qp_retired_after": False, "rkey": SYNTH_KEYS[gen],
           "address": SYNTH_ADDRESS, "length": SYNTH_LENGTH,
           "pd_handle": SYNTH_PD, "access": SYNTH_ACCESS,
           "owner_bytes_sha256_before": SYNTH_H0,
           "owner_bytes_sha256_after": SYNTH_H0,
           "expected_owner_bytes_sha256": SYNTH_H0,
           "guards_intact": True, "mono_ns": mono}
    rec.update(overrides)
    return rec


def _control_event(seq, control_kind, mono, acked=True,
                   descriptor_generation=None):
    return {"kind": "control-event", "seq": seq, "control_kind": control_kind,
            "acked": acked, "descriptor_generation": descriptor_generation,
            "mono_ns": mono}


def build_synthetic_run(root, run_role="pilot", label="synthetic",
                        stop_reason="count-budget"):
    """Write a complete synthetic pilot/main run (owner + requester ledgers).

    Key plan: gen3 reuses gen1's index with a novel key (an index-only
    recurrence), gen4 repeats gen2's full key.  gen1's descriptor is
    delivered to the requester before gen3 is advertised, gen2's before
    gen4, so both samples carry descriptor_received_before_matching_
    generation == True.  Expected analyzer results are returned alongside
    the paths so tests can assert against them.
    """
    import ledger  # implementation module under test

    owner_path = os.path.join(root, "%s-owner.ledger" % label)
    requester_path = os.path.join(root, "%s-requester.ledger" % label)

    mono = [1000]

    def m(step=10):
        mono[0] += step
        return mono[0]

    owner_records = [
        {"kind": "run-meta", "run_role": run_role, "label": label,
         "endpoint": "owner", "stop_reason": stop_reason,
         "allocation_outlived_all_qps": True, "mono_ns": m()},
        _registration(1, m()),
        _probe("pc-1", 1, "positive-control", m(),
               owner_bytes_sha256_after=SYNTH_H1,
               expected_owner_bytes_sha256=SYNTH_H1),
        _deregistration(1, m()),
        _registration(2, m()),
        _probe("pc-2", 2, "positive-control", m(),
               owner_bytes_sha256_after=SYNTH_H1,
               expected_owner_bytes_sha256=SYNTH_H1),
        _deregistration(2, m()),
        _registration(3, m()),
        _probe("pc-3", 3, "positive-control", m(),
               owner_bytes_sha256_after=SYNTH_H1,
               expected_owner_bytes_sha256=SYNTH_H1),
        # index-only recurrence: saved gen1 descriptor (same index 1,
        # different key) probed through a fresh, retiring QP while gen3's
        # MR is live; remote access error with intact memory -> rejected.
        _probe("io-3", 3, "index-only-probe", m(), rkey=SYNTH_KEYS[1],
               qp_role="fresh", qp_retired_after=True,
               wc_status=WC_REM_ACCESS_ERR),
        _deregistration(3, m()),
        _registration(4, m()),
        _probe("pc-4", 4, "positive-control", m(),
               owner_bytes_sha256_after=SYNTH_H2,
               expected_owner_bytes_sha256=SYNTH_H2),
        # full-key recurrence: identical bits; live probe succeeds and can
        # only resolve to the CURRENT registration.
        _probe("fk-4-live", 4, "full-key-live", m(), qp_role="original",
               owner_bytes_sha256_after=SYNTH_H2,
               expected_owner_bytes_sha256=SYNTH_H2),
        _deregistration(4, m()),
        # after quiescent deregistration the same values probed on a
        # separate fresh QP are rejected.
        _probe("fk-4-post", 4, "full-key-post-deregistration", m(),
               rkey=SYNTH_KEYS[4], qp_role="fresh",
               wc_status=WC_REM_ACCESS_ERR,
               owner_bytes_sha256_before=SYNTH_H2,
               owner_bytes_sha256_after=SYNTH_H2,
               expected_owner_bytes_sha256=SYNTH_H2),
    ]
    requester_records = [
        {"kind": "run-meta", "run_role": run_role, "label": label,
         "endpoint": "requester", "stop_reason": stop_reason, "mono_ns": m()},
        _control_event(1, "run-ready", m()),
        _control_event(11, "descriptor-received", m(),
                       descriptor_generation=1),
        _control_event(21, "descriptor-received", m(),
                       descriptor_generation=2),
        _control_event(31, "descriptor-received", m(),
                       descriptor_generation=3),
        _control_event(41, "descriptor-received", m(),
                       descriptor_generation=4),
        _control_event(51, "run-end", m()),
    ]

    with ledger.LedgerWriter(owner_path) as w:
        for i, rec in enumerate(owner_records):
            w.append(rec)
            if i % 3 == 2:
                w.checkpoint()
        w.checkpoint()
    with ledger.LedgerWriter(requester_path) as w:
        for rec in requester_records:
            w.append(rec)
        w.checkpoint()

    expected = {
        "run_role": run_role,
        "label": label,
        "stop_reason": stop_reason,
        "target_registrations_attempted": 4,
        "target_registrations_succeeded": 4,
        "calibration_prefix_registrations": 4,
        "index_recurrences_observed": 2,   # gen3 (index 1), gen4 (index 2)
        "full_key_recurrences_observed": 1,  # gen4 (rkey of gen2)
        "index_recurrence_events": [
            {"generation": 3, "index": 1, "first_earlier_generation": 1,
             "last_earlier_generation": 1, "matching_earlier_generations": 1,
             "denominator": 2},
            {"generation": 4, "index": 2, "first_earlier_generation": 2,
             "last_earlier_generation": 2, "matching_earlier_generations": 1,
             "denominator": 3},
        ],
        "full_key_recurrence_events": [
            {"generation": 4, "rkey": SYNTH_KEYS[4],
             "first_earlier_generation": 2, "last_earlier_generation": 2,
             "matching_earlier_generations": 1, "denominator": 3},
        ],
        "samples": [
            {"kind": "index-only", "generation": 3,
             "saved_descriptor": {"generation": 1, "rkey": SYNTH_KEYS[1],
                                  "index": 1, "low_byte": 0x11},
             "descriptor_received_before_matching_generation": True,
             "probes": [{"probe_id": "io-3",
                         "classification": "rejected"}]},
            {"kind": "full-key", "generation": 4,
             "saved_descriptor": {"generation": 2, "rkey": SYNTH_KEYS[2],
                                  "index": 2, "low_byte": 0x22},
             "descriptor_received_before_matching_generation": True,
             "probes": [{"probe_id": "fk-4-live",
                         "classification": "accepted"},
                        {"probe_id": "fk-4-post",
                         "classification": "rejected"}]},
        ],
    }
    return owner_path, requester_path, expected


def build_classification_fixture(root):
    """One synthetic generation with probes covering every classification
    rule; returns (owner_path, requester_path, expected_classifications)."""
    import ledger

    owner_path = os.path.join(root, "classif-owner.ledger")
    requester_path = os.path.join(root, "classif-requester.ledger")
    rkey = SYNTH_KEYS[1]
    probes = [
        # probe_id, overrides, expected classification
        ("t-timeout", {"timed_out": True, "wc_status": None},
         "indeterminate"),
        ("t-flush", {"wc_status": WC_WR_FLUSH_ERR}, "indeterminate"),
        ("t-transport", {"wc_status": WC_TRANSPORT_ERR}, "indeterminate"),
        ("t-retry", {"wc_status": WC_RETRY_EXC_ERR}, "indeterminate"),
        ("t-postfail", {"posting_rc": 12, "wc_status": None},
         "indeterminate"),
        ("t-reject", {"wc_status": WC_REM_ACCESS_ERR}, "rejected"),
        ("t-reject-bytes", {"wc_status": WC_REM_ACCESS_ERR,
                            "owner_bytes_sha256_after": SYNTH_H1},
         "anomalous"),
        ("t-reject-guards", {"wc_status": WC_REM_ACCESS_ERR,
                             "guards_intact": False}, "anomalous"),
        ("t-accept", {"wc_status": WC_SUCCESS,
                      "owner_bytes_sha256_after": SYNTH_H3,
                      "expected_owner_bytes_sha256": SYNTH_H3}, "accepted"),
        ("t-accept-wrongbytes", {"wc_status": WC_SUCCESS,
                                 "owner_bytes_sha256_after": SYNTH_H1,
                                 "expected_owner_bytes_sha256": SYNTH_H3},
         "anomalous"),
    ]
    mono = [1000]

    def m(step=10):
        mono[0] += step
        return mono[0]

    owner_records = [
        {"kind": "run-meta", "run_role": "pilot", "label": "classification",
         "endpoint": "owner", "stop_reason": "explicit-stop",
         "allocation_outlived_all_qps": True, "mono_ns": m()},
        _registration(1, m()),
    ]
    expected = {}
    for probe_id, overrides, classification in probes:
        owner_records.append(_probe(probe_id, 1, "positive-control", m(),
                                    **overrides))
        expected[probe_id] = classification
    owner_records.append(_deregistration(1, m()))

    with ledger.LedgerWriter(owner_path) as w:
        for rec in owner_records:
            w.append(rec)
        w.checkpoint()
    with ledger.LedgerWriter(requester_path) as w:
        w.append({"kind": "run-meta", "run_role": "pilot",
                  "label": "classification", "endpoint": "requester",
                  "stop_reason": "explicit-stop", "mono_ns": m()})
        w.append(_control_event(1, "run-ready", m()))
        w.checkpoint()
    return owner_path, requester_path, expected


def build_synthetic_manifest_tree(root):
    """Build a complete miniature experiment tree under ``root`` that must
    pass ``python3 verify.py --strict <root>/run-manifest.json``.

    Returns the manifest path.  Uses the real ledger and analyzer modules
    so the synthetic analysis artifact is their genuine output.
    """
    import analyzer

    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    os.makedirs(os.path.join(root, "docs"), exist_ok=True)
    os.makedirs(os.path.join(root, "results"), exist_ok=True)
    os.makedirs(os.path.join(root, "results", "pilot"), exist_ok=True)
    os.makedirs(os.path.join(root, "results", "main"), exist_ok=True)

    artifacts = []

    def add(rel, data, role, run=None, mode="w"):
        path = os.path.join(root, rel)
        with open(path, mode) as f:
            f.write(data)
        artifacts.append({"role": role, "path": rel,
                          "sha256": sha256_file(path),
                          "bytes": os.path.getsize(path)})
        if run:
            artifacts[-1]["run"] = run
        return path

    # harness source / binary / build log
    add("src/synth.c", "int main(void){return 0;}\n", "harness-source")
    binary_path = os.path.join(root, "synth.bin")
    with open(binary_path, "wb") as f:
        f.write(b"\x7fELF-synthetic")
    artifacts.append({"role": "binary", "path": "synth.bin",
                      "sha256": sha256_file(binary_path),
                      "bytes": os.path.getsize(binary_path)})
    add("build-log.json", json.dumps({
        "command": "gcc -std=c11 -O2 -o synth.bin src/synth.c",
        "exit_code": 0, "stdout": "", "stderr": "",
        "binary_sha256": sha256_file(binary_path)}), "build-log")

    # environments, remote commands, schema doc, negative checks
    for role, host in (("environment-owner", OWNER_ENDPOINT),
                       ("environment-requester", REQUESTER_ENDPOINT)):
        add("%s.json" % role, json.dumps({
            "host": host, "kernel_version": "6.8.0-147-generic",
            "rxe0_active": True}), role)
    add("remote-commands.json", json.dumps([
        {"host": REQUESTER_ENDPOINT,
         "command": "scp synth.bin root@%s:%s/" % (OWNER_ENDPOINT,
                                                   REMOTE_DIRECTORY)},
        {"host": OWNER_ENDPOINT,
         "command": "%s/owner --role owner" % REMOTE_DIRECTORY}]),
        "remote-commands")
    add("docs/ledger-schema.md",
        "Binary ledger schema: magic, version, fixed-size records, "
        "per-record checksum and trailing checksum, periodic checkpoint "
        "every N records. Fields: generation, rkey (full 32-bit key), "
        "index, low byte, register/deregister return codes, timestamps "
        "(mono_ns), probe posting rc / WC status / QP state / owner bytes "
        "/ guards / PD / address / access.", "ledger-schema")
    add("results/negative-checks.json", json.dumps({
        "checks": [{"check": "ledger-tamper-rejected", "passed": True},
                   {"check": "analyzer-tamper-rejected", "passed": True},
                   {"check": "verify-bad-hash-rejected", "passed": True}]}),
        "negative-checks")

    # synthetic runs (pilot + main) and their artifacts
    ledger_paths = []
    for run_role in ("pilot", "main"):
        owner, requester, _exp = build_synthetic_run(
            root, run_role=run_role, label="synthetic-%s" % run_role)
        ledger_paths.extend([owner, requester])
        sub = "results/%s" % run_role
        for role, src in (("owner-ledger", owner), ("requester-ledger",
                                                    requester)):
            rel = "%s/%s" % (sub, os.path.basename(src))
            os.makedirs(os.path.dirname(os.path.join(root, rel)),
                        exist_ok=True)
            with open(src, "rb") as f_in, \
                    open(os.path.join(root, rel), "wb") as f_out:
                f_out.write(f_in.read())
            path = os.path.join(root, rel)
            artifacts.append({"role": role, "path": rel,
                              "sha256": sha256_file(path),
                              "bytes": os.path.getsize(path),
                              "run": run_role})
        add("%s/owner.jsonl" % sub, "{}\n", "raw-logs", run=run_role)
        add("%s/owner-diagnostics.json" % sub, json.dumps({
            "available": True, "new_abnormal_lines": []}),
            "diagnostics", run=run_role)
        add("%s/run-config.json" % sub, json.dumps({
            "max_wall_seconds": BUDGETS["max_wall_seconds"],
            "max_target_registrations":
                BUDGETS["max_target_registrations"]}),
            "run-config", run=run_role)
        add("%s/stop-record.json" % sub, json.dumps({
            "stop_reason": "count-budget", "registrations_at_stop": 4,
            "wall_seconds_at_stop": 0.5}), "stop-record", run=run_role)

    # analysis artifact: genuine analyzer output over the synthetic ledgers
    analysis = analyzer.analyze(ledger_paths)
    add("results/analysis.json",
        json.dumps(analysis, indent=2, sort_keys=True) + "\n", "analysis")
    add("results/report.md",
        "# Synthetic report placeholder\nobserved indeterminate\n",
        "report")

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "experiment": {
            "name": "bounded-rxe-registration-churn",
            "owner_endpoint": OWNER_ENDPOINT,
            "requester_endpoint": REQUESTER_ENDPOINT,
            "remote_directory": REMOTE_DIRECTORY,
        },
        "config": {
            "allocation_bytes": ALLOCATION_BYTES,
            "probe": dict(PROBE_SPEC),
            "mr_access": sorted(REQUIRED_ACCESS_FLAGS),
            "fixed_allocation": True,
            "allocation_alive_until_qps_destroyed": True,
            "quiesce_before_deregistration": True,
        },
        "budgets": dict(BUDGETS),
        "prohibitions": sorted(REQUIRED_PROHIBITIONS),
        "sample_plan": {"index_only": SAMPLE_PLAN["index_only"],
                        "full_key": SAMPLE_PLAN["full_key"],
                        "positive_stopping_rule":
                            SAMPLE_PLAN["positive_stopping_rule"]},
        "runs": [
            {"role": "pilot", "label": "synthetic-pilot",
             "stop_reason": "count-budget"},
            {"role": "main", "label": "synthetic-main",
             "stop_reason": "count-budget"},
        ],
        "artifacts": artifacts,
    }
    manifest_path = os.path.join(root, "run-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    return manifest_path


def run_verify(manifest_path, strict=True, timeout=300):
    cmd = [sys.executable, os.path.join(REPO_ROOT, "verify.py")]
    if strict:
        cmd.append("--strict")
    cmd.append(manifest_path)
    return subprocess.run(cmd, cwd=REPO_ROOT, timeout=timeout,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


def run_analyzer_cli(out_dir, ledger_paths, timeout=600):
    cmd = [sys.executable, os.path.join(REPO_ROOT, "analyzer.py"),
           "--out", out_dir] + list(ledger_paths)
    return subprocess.run(cmd, cwd=REPO_ROOT, timeout=timeout,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
