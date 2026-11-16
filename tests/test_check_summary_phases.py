"""Focused tests for the optional "Phase outcomes" column of
scripts/check_summary.py: the phase-specific probe outcomes (4 rejected
index-only probes, 8 accepted live full-key probes, 8 rejected
post-deregistration probes, plus accepted positive controls) must
recompute exactly from the analysis, so the sample-level overall summary
cannot conceal observed live writes."""

import json
import os
import shutil
import tempfile
import unittest

import support

PHASE_COLUMN = "Phase outcomes"


def recompute_phase_cell(run):
    """Independent recompute of the expected Phase outcomes cell."""
    if not run.get("samples"):
        return "none"
    counts = {}
    for sample in run["samples"]:
        for probe in sample["probes"]:
            token = {"index-only-probe": "io",
                     "full-key-live": "fk-live",
                     "full-key-post-deregistration": "fk-post"}[
                probe["phase"]]
            key = (token, probe["classification"])
            counts[key] = counts.get(key, 0) + 1
        key = ("positive", sample["positive_control"]["classification"])
        counts[key] = counts.get(key, 0) + 1
    order = {"io": 0, "fk-live": 1, "fk-post": 2, "positive": 3}
    classes = {"accepted": 0, "rejected": 1, "indeterminate": 2,
               "anomalous": 3}
    tokens = ["%s %s=%d" % (token, classification, count)
              for (token, classification), count in counts.items()]
    tokens.sort(key=lambda t: (order[t.split()[0]],
                               classes[t.split()[1].split("=")[0]]))
    return "; ".join(tokens)


def with_phase_column(table, cells_by_label):
    """Splice a Phase outcomes column (appended at the end) into a
    rendered 18-column table."""
    lines = []
    for line in table.splitlines():
        stripped = line.rstrip()
        if line.startswith("| Run | Role |"):
            line = stripped[:-1].rstrip() + " | " + PHASE_COLUMN + " |"
        elif line.startswith("|--"):
            line = stripped[:-1] + "---|"
        elif line.startswith("|"):
            label = line.split("|")[1].strip()
            cell = cells_by_label.get(label, "none")
            line = stripped[:-1].rstrip() + " | " + cell + " |"
        lines.append(line)
    return "\n".join(lines) + "\n"


class PhaseOutcomesColumnTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import analyzer
        cls.tmp = tempfile.mkdtemp(prefix="churn-summary-phases-")
        smoke = support.build_synthetic_run(
            cls.tmp, run_role="smoke", label="shakedown-synthetic",
            stop_reason="count-budget", mono_base=1000)
        main = support.build_synthetic_run(
            cls.tmp, run_role="main", label="synthetic-main",
            stop_reason="count-budget", mono_base=10 ** 10)
        cls.analysis = analyzer.analyze([smoke[0], smoke[1],
                                         main[0], main[1]])
        cls.analysis_path = os.path.join(cls.tmp, "analysis.json")
        with open(cls.analysis_path, "w", encoding="utf-8") as f:
            json.dump(cls.analysis, f, indent=2, sort_keys=True)
            f.write("\n")
        cls.table = support.render_results_table(cls.analysis)
        runs = {r["run_role"]: r for r in cls.analysis["runs"]}
        # fixture phase truth: one rejected io probe, one accepted live
        # full-key probe, one rejected post-dereg probe, accepted controls
        cls.expected = {runs["main"]["label"]:
                        recompute_phase_cell(runs["main"]),
                        runs["smoke"]["label"]:
                        recompute_phase_cell(runs["smoke"])}
        cls.main_label = runs["main"]["label"]
        cls.smoke_label = runs["smoke"]["label"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _check(self, table_text):
        md_path = os.path.join(self.tmp, "reproduction-results.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# Reproduction results\n\n" + table_text)
        return support.run_check_summary(md_path, self.analysis_path)

    def test_fixture_phase_truth_is_mixed(self):
        # premise guard: the synthetic main run really has an ACCEPTED
        # live full-key probe that the overall sample summary hides
        main_run = [r for r in self.analysis["runs"]
                    if r["run_role"] == "main"][0]
        live = [p for s in main_run["samples"] for p in s["probes"]
                if p["phase"] == "full-key-live"]
        self.assertTrue(live)
        self.assertEqual([p["classification"] for p in live],
                         ["accepted"] * len(live))
        self.assertIn("fk-live accepted=", self.expected[self.main_label])
        # the synthetic smoke fixture also carries samples, so its cell is
        # a recomputed token list too (never fabricated)
        self.assertTrue(self.expected[self.smoke_label].startswith("io "))

    def test_accepts_correct_phase_column(self):
        result = self._check(with_phase_column(self.table, self.expected))
        self.assertEqual(
            result.returncode, 0,
            "check_summary rejected a correct Phase outcomes column:\n%s"
            % result.stderr.decode(errors="replace"))

    def test_accepts_table_without_phase_column(self):
        # backward compatibility with the frozen 18-column format
        result = self._check(self.table)
        self.assertEqual(result.returncode, 0)

    def _assert_rejected(self, cells):
        result = self._check(with_phase_column(self.table, cells))
        self.assertNotEqual(
            result.returncode, 0,
            "check_summary accepted a defective Phase outcomes column")

    def test_rejects_wrong_phase_count(self):
        cells = dict(self.expected)
        cells[self.main_label] = cells[self.main_label].replace(
            "io rejected=", "io rejected=3")
        # keep the count internally consistent-looking but wrong vs data
        self._assert_rejected(cells)

    def test_rejects_wrong_phase_classification(self):
        cells = dict(self.expected)
        cells[self.main_label] = cells[self.main_label].replace(
            "fk-live accepted=", "fk-live rejected=")
        self._assert_rejected(cells)

    def test_rejects_dropped_phase_token(self):
        cells = dict(self.expected)
        tokens = [t for t in cells[self.main_label].split("; ")]
        cells[self.main_label] = "; ".join(
            t for t in tokens if not t.startswith("fk-post"))
        self._assert_rejected(cells)

    def test_rejects_fabricated_phase_token(self):
        cells = dict(self.expected)
        cells[self.main_label] += "; io accepted=1"
        self._assert_rejected(cells)

    def test_rejects_malformed_phase_token(self):
        cells = dict(self.expected)
        cells[self.main_label] = "index-only: 4 rejected"
        self._assert_rejected(cells)

    def test_rejects_none_for_run_with_samples(self):
        cells = dict(self.expected)
        cells[self.main_label] = "none"
        self._assert_rejected(cells)

    def test_rejects_phases_for_run_without_samples(self):
        # a run with zero samples must carry "none", not fabricated
        # phase counts (mirrors the real shakedown run)
        analysis = {
            "runs": [
                {"run_role": "main", "label": "sampleless-main",
                 "stop_reason": "count-budget",
                 "target_registrations_succeeded": 4,
                 "wall_seconds_first_registration_to_final_cleanup": 0.0,
                 "index_recurrences_observed": 2,
                 "full_key_recurrences_observed": 1,
                 "samples": []}],
            "aggregate": {
                "target_registrations_succeeded_total": 4,
                "aggregate_max_target_registrations": 604096,
                "within_registration_budget": True,
                "wall_seconds_first_registration_to_final_cleanup": 0.0,
                "max_wall_seconds": 900,
                "within_wall_budget": True},
        }
        table = with_phase_column(
            support.render_results_table(analysis),
            {"sampleless-main": "io rejected=1"})
        analysis_path = os.path.join(self.tmp, "analysis-sampleless.json")
        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, sort_keys=True)
            f.write("\n")
        md_path = os.path.join(self.tmp, "reproduction-results.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# Reproduction results\n\n" + table)
        result = support.run_check_summary(md_path, analysis_path)
        self.assertNotEqual(
            result.returncode, 0,
            "a fabricated phase cell for a sampleless run must be "
            "rejected")

    def test_rejects_unrelated_extra_column(self):
        table = self.table.replace(
            "| Limitations |", "| Limitations | Notes |", 1)
        lines = []
        for line in table.splitlines():
            if line.startswith("|") and not line.startswith("|--"):
                line += " x |"
            elif line.startswith("|--"):
                line += "--|"
            lines.append(line)
        self._assert_rejected_marker("\n".join(lines) + "\n")

    def _assert_rejected_marker(self, table_text):
        result = self._check(table_text)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
