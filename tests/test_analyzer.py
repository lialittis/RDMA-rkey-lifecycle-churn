"""Acceptance tests for the replayable offline analyzer (analyzer.py):
determinism, archived-analysis reproducibility, independent recount of
registration/index/full-key recurrence statistics, sample validation and
classification rules."""

import os
import shutil
import tempfile
import unittest

import support
from support import (ANALYSIS_SCHEMA, BUDGETS, CLASSIFICATIONS,
                     SAMPLE_PLAN, ROLE_REG_CAPS, PRIMARY_ROLE,
                     AUXILIARY_ROLE)


def _recount_recurrences(owner_records):
    """Independent recompute of recurrence events from target registrations.

    Returns (index_events, full_key_events) using the contract's rules:
    comparisons are against ALL earlier target registration generations
    (denominator = number of earlier generations).
    """
    regs = [r for r in owner_records
            if r.get("kind") == "registration"
            and r.get("target") is True
            and r.get("register_rc") == 0]
    regs.sort(key=lambda r: r["generation"])
    seen_index = {}
    seen_rkey = {}
    index_events = []
    full_key_events = []
    for rec in regs:
        gen = rec["generation"]
        index = rec["index"]
        rkey = rec["rkey"]
        denominator = gen - 1  # contiguous 1-based generations
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
    return index_events, full_key_events


def _project(record, keys):
    return {k: record[k] for k in keys if k in record}


INDEX_EVENT_KEYS = ("generation", "index", "first_earlier_generation",
                    "last_earlier_generation", "matching_earlier_generations",
                    "denominator")
FULL_KEY_EVENT_KEYS = ("generation", "rkey", "first_earlier_generation",
                       "last_earlier_generation",
                       "matching_earlier_generations", "denominator")


class AnalyzerApiTests(unittest.TestCase):

    def test_module_importable_with_analyze(self):
        import analyzer
        self.assertTrue(callable(analyzer.analyze))

    def test_cli_writes_analysis_json(self):
        manifest = support.load_manifest()
        paths = support.all_ledger_paths_in_order(manifest,
                                                  support.REPO_ROOT)
        self.assertTrue(paths)
        with tempfile.TemporaryDirectory() as tmp:
            result = support.run_analyzer_cli(tmp, paths)
            self.assertEqual(result.returncode, 0,
                             "analyzer CLI failed:\n%s\n%s"
                             % (result.stdout.decode(), result.stderr.decode()))
            out = os.path.join(tmp, "analysis.json")
            self.assertTrue(os.path.isfile(out))
            analysis = support.load_json(out)
            self.assertEqual(analysis.get("schema"), ANALYSIS_SCHEMA)

    def test_cli_output_deterministic(self):
        manifest = support.load_manifest()
        paths = support.all_ledger_paths_in_order(manifest,
                                                  support.REPO_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            out1 = os.path.join(tmp, "a")
            out2 = os.path.join(tmp, "b")
            os.makedirs(out1)
            os.makedirs(out2)
            r1 = support.run_analyzer_cli(out1, paths)
            r2 = support.run_analyzer_cli(out2, paths)
            self.assertEqual(r1.returncode, 0)
            self.assertEqual(r2.returncode, 0)
            with open(os.path.join(out1, "analysis.json"), "rb") as f:
                first = f.read()
            with open(os.path.join(out2, "analysis.json"), "rb") as f:
                second = f.read()
            self.assertEqual(first, second,
                             "analyzer output is not byte-deterministic")

    def test_cli_rejects_tampered_ledger(self):
        import ledger
        manifest = support.load_manifest()
        paths = support.all_ledger_paths_in_order(manifest,
                                                  support.REPO_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            copies = []
            for i, src in enumerate(paths):
                copy = os.path.join(tmp, "copy-%d.ledger" % i)
                shutil.copyfile(src, copy)
                copies.append(copy)
            raw = bytearray(support.read_bytes(copies[0]))
            raw[len(raw) // 2] ^= 0x02
            with open(copies[0], "wb") as f:
                f.write(bytes(raw))
            result = support.run_analyzer_cli(tmp, copies)
            self.assertNotEqual(result.returncode, 0,
                                "analyzer must reject a corrupt ledger "
                                 "instead of analyzing it")


class ArchivedAnalysisTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manifest = support.load_manifest()
        cls.manifest_dir = support.REPO_ROOT
        cls.ledger_paths = support.all_ledger_paths_in_order(
            cls.manifest, cls.manifest_dir)
        import analyzer
        cls.analysis = analyzer.analyze(cls.ledger_paths)

    def test_analysis_schema_and_runs(self):
        self.assertEqual(self.analysis.get("schema"), ANALYSIS_SCHEMA)
        runs = self.analysis.get("runs")
        self.assertIsInstance(runs, list)
        self.assertGreaterEqual(len(runs), 1)
        manifest_roles = [r["role"] for r in
                          support.run_entries(self.manifest)]
        analysis_roles = [r.get("run_role") for r in runs]
        for role in manifest_roles:
            self.assertIn(role, analysis_roles,
                          "manifest run %r missing from analysis" % role)

    def test_archived_analysis_artifact_replays_identically(self):
        arts = support.artifacts_by_role(self.manifest, "analysis")
        self.assertEqual(len(arts), 1,
                         "exactly one analysis artifact expected")
        archived = support.load_json(
            support.artifact_fs_path(self.manifest_dir, arts[0]))
        self.assertEqual(archived, self.analysis,
                         "archived analysis.json does not replay from the "
                         "archived ledgers")

    def test_headline_is_main_run(self):
        runs = self.analysis["runs"]
        main_runs = [r for r in runs if r.get("run_role") == "main"]
        if main_runs:
            self.assertEqual(self.analysis.get("headline"), main_runs[0])
        else:
            self.assertIsNone(self.analysis.get("headline"))
            self.assertIsInstance(
                self.analysis.get("headline_absent_reason"), str)

    def test_run_object_required_fields_and_budgets(self):
        for run in self.analysis["runs"]:
            for field in ("run_role", "label", "stop_reason",
                          "target_registrations_attempted",
                          "target_registrations_succeeded",
                          "wall_seconds_first_registration_to_final_cleanup",
                          "quiesce_before_every_deregistration",
                          "allocation_invariants_preserved",
                          "index_recurrences_observed",
                          "full_key_recurrences_observed",
                          "index_recurrence_events",
                          "full_key_recurrence_events",
                          "samples", "probe_classifications"):
                self.assertIn(field, run,
                              "analysis run %r missing %r"
                              % (run.get("label"), field))
            self.assertIn(run["run_role"], ("shakedown", "main"),
                          "the frozen revision's pilot role must not "
                          "appear in a churn-analysis/2 run object")
            self.assertIsInstance(run["target_registrations_succeeded"], int)
            self.assertGreaterEqual(run["target_registrations_succeeded"], 0)
            self.assertLessEqual(
                run["target_registrations_succeeded"],
                ROLE_REG_CAPS[run["run_role"]],
                "run %r exceeds its role registration cap"
                % run.get("label"))
            self.assertLessEqual(
                run["wall_seconds_first_registration_to_final_cleanup"],
                float(BUDGETS["max_wall_seconds"]),
                "hard 900 s budget exceeded")
            self.assertIs(run["quiesce_before_every_deregistration"], True)
            self.assertIs(run["allocation_invariants_preserved"], True)
            # full-key events are a subset of index events
            self.assertLessEqual(run["full_key_recurrences_observed"],
                                 run["index_recurrences_observed"])
            self.assertEqual(run["index_recurrences_observed"],
                             len(run["index_recurrence_events"]))
            self.assertEqual(run["full_key_recurrences_observed"],
                             len(run["full_key_recurrence_events"]))

    def test_aggregate_block_consistent_with_runs(self):
        aggregate = self.analysis.get("aggregate")
        self.assertIsInstance(aggregate, dict,
                              "analysis must carry an aggregate object "
                              "covering every attempt of the run plan")
        total = sum(run["target_registrations_succeeded"]
                    for run in self.analysis["runs"])
        self.assertEqual(
            aggregate.get("target_registrations_succeeded_total"), total,
            "aggregate total must be the sum over runs (the shakedown is "
            "never merged into the main run object, only summed here)")
        self.assertLessEqual(
            aggregate.get("target_registrations_succeeded_total", 1 << 60),
            BUDGETS["aggregate_max_target_registrations"],
            "hard aggregate ceiling of 604096 successful target "
            "registrations exceeded across all attempts")
        self.assertEqual(
            aggregate.get("aggregate_max_target_registrations"),
            BUDGETS["aggregate_max_target_registrations"])
        self.assertIs(aggregate.get("within_registration_budget"), True,
                      "the archived attempts exceed the aggregate "
                      "registration budget")
        self.assertEqual(aggregate.get("max_wall_seconds"),
                         BUDGETS["max_wall_seconds"])
        span = aggregate.get(
            "wall_seconds_first_registration_to_final_cleanup")
        if span is not None:
            self.assertLessEqual(
                span, float(BUDGETS["max_wall_seconds"]),
                "aggregate wall span from the first registration through "
                "final cleanup exceeds 900 s")
            self.assertIs(aggregate.get("within_wall_budget"), True)
        else:
            self.assertIsNone(aggregate.get("within_wall_budget"))

    def test_recurrence_counts_replay_from_ledger(self):
        import ledger
        for run, owner, requester in support.run_ledger_paths(
                self.manifest, self.manifest_dir):
            records = ledger.read_ledger(owner).records
            index_events, full_key_events = _recount_recurrences(records)
            analysis_run = None
            for candidate in self.analysis["runs"]:
                if candidate["run_role"] == run["role"]:
                    analysis_run = candidate
            self.assertIsNotNone(analysis_run,
                                 "run %r absent from analysis" % run["role"])
            self.assertEqual(
                analysis_run["index_recurrences_observed"],
                len(index_events),
                "index recurrence count does not replay for run %r"
                % run["label"])
            self.assertEqual(
                analysis_run["full_key_recurrences_observed"],
                len(full_key_events),
                "full-key recurrence count does not replay for run %r"
                % run["label"])
            self.assertEqual(
                [_project(e, INDEX_EVENT_KEYS)
                 for e in analysis_run["index_recurrence_events"]],
                index_events,
                "index recurrence events (first/last earlier IDs, "
                "denominators) do not replay")
            self.assertEqual(
                [_project(e, FULL_KEY_EVENT_KEYS)
                 for e in analysis_run["full_key_recurrence_events"]],
                full_key_events,
                "full-key recurrence events do not replay")

    def test_samples_within_predeclared_plan_and_well_formed(self):
        import ledger
        for run, owner, requester in support.run_ledger_paths(
                self.manifest, self.manifest_dir):
            records = ledger.read_ledger(owner).records
            regs = {r["generation"]: r for r in records
                    if r.get("kind") == "registration"
                    and r.get("target") is True
                    and r.get("register_rc") == 0}
            # first-seen maps establish which earlier generation a
            # recurring index/rkey can point back to
            index_first_seen = {}
            rkey_first_seen = {}
            for rec in sorted(regs.values(),
                              key=lambda r: r["generation"]):
                index_first_seen.setdefault(rec["index"],
                                            rec["generation"])
                rkey_first_seen.setdefault(rec["rkey"], rec["generation"])

            analysis_run = None
            for candidate in self.analysis["runs"]:
                if candidate["run_role"] == run["role"]:
                    analysis_run = candidate
            samples = analysis_run["samples"]
            index_only = [s for s in samples if s["kind"] == "index-only"]
            full_key = [s for s in samples if s["kind"] == "full-key"]
            self.assertLessEqual(len(index_only),
                                 SAMPLE_PLAN["index_only"])
            self.assertLessEqual(len(full_key),
                                 SAMPLE_PLAN["full_key"])
            gens = [s["generation"] for s in index_only]
            self.assertEqual(gens, sorted(gens))
            gens = [s["generation"] for s in full_key]
            self.assertEqual(gens, sorted(gens))

            for sample in samples:
                gen = sample["generation"]
                self.assertIn(gen, regs,
                              "sample generation has no registration")
                current = regs[gen]
                self.assertEqual(sample["current_rkey"], current["rkey"])
                self.assertEqual(sample["current_index"], current["index"])
                self.assertEqual(sample["current_low_byte"],
                                 current["low_byte"])
                saved = sample["saved_descriptor"]
                self.assertLess(saved["generation"], gen)
                self.assertIn(saved["generation"], regs)
                self.assertIsInstance(
                    sample["descriptor_received_before_matching_generation"],
                    bool)
                self.assertEqual(sample["positive_control"]["classification"],
                                 "accepted",
                                 "every classified sample needs a "
                                 "successful positive control")
                self.assertIn(sample["classification"], CLASSIFICATIONS)
                for probe in sample["probes"]:
                    self.assertIn(probe["classification"], CLASSIFICATIONS)
                    for field in ("posting_rc", "wc_status", "qp_role",
                                  "qp_state", "rkey", "address", "length",
                                  "pd_handle", "access",
                                  "owner_bytes_sha256_before",
                                  "owner_bytes_sha256_after", "guards_intact"):
                        self.assertIn(field, probe,
                                      "sample probe missing separated "
                                      "field %r" % field)
                if sample["kind"] == "index-only":
                    self.assertEqual(saved["index"], current["index"])
                    self.assertNotEqual(saved["rkey"], current["rkey"])
                    self.assertEqual(index_first_seen[current["index"]],
                                     saved["generation"])
                    self.assertEqual(len(sample["probes"]), 1)
                    probe = sample["probes"][0]
                    self.assertEqual(probe["phase"], "index-only-probe")
                    self.assertEqual(probe["qp_role"], "fresh")
                    self.assertIs(probe["qp_retired_after"], True)
                else:
                    self.assertEqual(saved["rkey"], current["rkey"])
                    self.assertEqual(rkey_first_seen[current["rkey"]],
                                     saved["generation"])
                    self.assertEqual(len(sample["probes"]), 2)
                    phases = {p["phase"] for p in sample["probes"]}
                    self.assertEqual(
                        phases, {"full-key-live",
                                 "full-key-post-deregistration"})
                    for probe in sample["probes"]:
                        if probe["phase"] == "full-key-post-deregistration":
                            self.assertEqual(probe["qp_role"], "fresh")
                    self.assertEqual(sample["current_rkey"],
                                     current["rkey"])


class SyntheticAnalyzerBehaviorTests(unittest.TestCase):
    """Analyzer correctness on fabricated fixtures (never counted as
    observations): classification rules and recurrence arithmetic."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="churn-analyzer-fixtures-")
        cls.classif_owner, cls.classif_requester, \
            cls.expected_classif = support.build_classification_fixture(
                cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_classification_rules(self):
        import analyzer
        analysis = analyzer.analyze([self.classif_owner,
                                     self.classif_requester])
        run = analysis["runs"][0]
        classifications = run["probe_classifications"]
        for probe_id, expected in self.expected_classif.items():
            self.assertIn(probe_id, classifications,
                          "probe %r unclassified" % probe_id)
            self.assertEqual(classifications[probe_id], expected,
                             "probe %r classified %r, expected %r"
                             % (probe_id, classifications[probe_id],
                                expected))

    def test_recurrence_arithmetic_and_samples(self):
        import analyzer
        owner, requester, expected = support.build_synthetic_run(
            self.tmp, run_role="main", label="synthetic-main2",
            stop_reason="count-budget")
        analysis = analyzer.analyze([owner, requester])
        run = analysis["runs"][0]
        self.assertEqual(run["target_registrations_attempted"],
                         expected["target_registrations_attempted"])
        self.assertEqual(run["target_registrations_succeeded"],
                         expected["target_registrations_succeeded"])
        self.assertEqual(
            [_project(e, INDEX_EVENT_KEYS)
             for e in run["index_recurrence_events"]],
            expected["index_recurrence_events"])
        self.assertEqual(
            [_project(e, FULL_KEY_EVENT_KEYS)
             for e in run["full_key_recurrence_events"]],
            expected["full_key_recurrence_events"])
        self.assertEqual(run["index_recurrences_observed"],
                         expected["index_recurrences_observed"])
        self.assertEqual(run["full_key_recurrences_observed"],
                         expected["full_key_recurrences_observed"])
        samples = run["samples"]
        self.assertEqual(len(samples), 2)
        by_kind = {}
        for sample in samples:
            by_kind.setdefault(sample["kind"], []).append(sample)
        for sample in by_kind.get("index-only", []):
            expected_sample = expected["samples"][0]
            self.assertEqual(sample["generation"],
                             expected_sample["generation"])
            self.assertEqual(sample["saved_descriptor"],
                             expected_sample["saved_descriptor"])
            self.assertIs(
                sample["descriptor_received_before_matching_generation"],
                True)
            self.assertEqual(sample["probes"][0]["classification"],
                             expected_sample["probes"][0]["classification"])
        for sample in by_kind.get("full-key", []):
            expected_sample = expected["samples"][1]
            self.assertEqual(sample["generation"],
                             expected_sample["generation"])
            self.assertEqual(sample["saved_descriptor"],
                             expected_sample["saved_descriptor"])
            probe_classes = sorted(
                p["classification"] for p in sample["probes"])
            self.assertEqual(
                probe_classes,
                sorted(p["classification"]
                       for p in expected_sample["probes"]))

    def test_shakedown_never_merged_into_main_denominators(self):
        import analyzer
        shakedown = support.build_synthetic_run(
            self.tmp, run_role="shakedown", label="sep-shakedown",
            stop_reason="count-budget", mono_base=1000)
        main = support.build_synthetic_run(
            self.tmp, run_role="main", label="sep-main",
            stop_reason="count-budget", mono_base=10 ** 10)
        combined = analyzer.analyze([shakedown[0], shakedown[1],
                                     main[0], main[1]])
        main_only = analyzer.analyze([main[0], main[1]])
        self.assertEqual([r["run_role"] for r in combined["runs"]],
                         ["shakedown", "main"])
        combined_main = [r for r in combined["runs"]
                         if r["run_role"] == "main"][0]
        # every denominator of the primary run is unaffected by the
        # auxiliary shakedown run that ran earlier
        for field in ("target_registrations_attempted",
                      "target_registrations_succeeded",
                      "index_recurrences_observed",
                      "full_key_recurrences_observed",
                      "index_recurrence_events",
                      "full_key_recurrence_events", "samples"):
            self.assertEqual(combined_main[field],
                             main_only["runs"][0][field],
                             "shakedown leaked into the main run's %r"
                             % field)
        # headline stays the main run object
        self.assertEqual(combined["headline"], combined_main)

    def test_aggregate_arithmetic_across_runs(self):
        import analyzer
        shakedown = support.build_synthetic_run(
            self.tmp, run_role="shakedown", label="agg-shakedown",
            stop_reason="count-budget", mono_base=1000)
        main = support.build_synthetic_run(
            self.tmp, run_role="main", label="agg-main",
            stop_reason="count-budget", mono_base=10 ** 10)
        analysis = analyzer.analyze([shakedown[0], shakedown[1],
                                     main[0], main[1]])
        aggregate = analysis["aggregate"]
        total = sum(r["target_registrations_succeeded"]
                    for r in analysis["runs"])
        self.assertEqual(aggregate["target_registrations_succeeded_total"],
                         total)
        self.assertEqual(total, 8)
        self.assertIs(aggregate["within_registration_budget"], True)
        self.assertEqual(aggregate["aggregate_max_target_registrations"],
                         BUDGETS["aggregate_max_target_registrations"])
        # owner monotonic span: from the shakedown's first registration to
        # the main run's final cleanup record, across both attempts
        import ledger
        shakedown_first = min(
            r["mono_ns"] for r in
            ledger.read_ledger(shakedown[0]).records
            if r.get("kind") == "registration" and r.get("target"))
        main_last = max(
            r["mono_ns"] for r in ledger.read_ledger(main[0]).records
            if isinstance(r.get("mono_ns"), int))
        expected_span = (main_last - shakedown_first) / 1e9
        self.assertAlmostEqual(
            aggregate["wall_seconds_first_registration_to_final_cleanup"],
            round(expected_span, 6), places=4)
        self.assertIs(aggregate["within_wall_budget"], True)

    def test_rejects_pilot_run_role_ledger(self):
        import analyzer
        import ledger
        pilot = support.build_synthetic_run(
            self.tmp, run_role="pilot", label="legacy-pilot",
            stop_reason="count-budget")
        with self.assertRaises(ledger.LedgerIntegrityError):
            analyzer.analyze([pilot[0], pilot[1]])


if __name__ == "__main__":
    unittest.main()
