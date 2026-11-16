"""Acceptance tests for scripts/check_summary.py: the committed results
table must machine-checkably agree with the raw archived analysis."""

import json
import os
import shutil
import tempfile
import unittest

import support


class CheckSummarySyntheticTests(unittest.TestCase):
    """check_summary.py must accept a correctly rendered table and reject
    every disagreement with the analysis (or structural defect)."""

    @classmethod
    def setUpClass(cls):
        import analyzer
        cls.tmp = tempfile.mkdtemp(prefix="churn-summary-synth-")
        shakedown = support.build_synthetic_run(
            cls.tmp, run_role="shakedown", label="synthetic-shakedown",
            stop_reason="count-budget", mono_base=1000)
        main = support.build_synthetic_run(
            cls.tmp, run_role="main", label="synthetic-main",
            stop_reason="count-budget", mono_base=10 ** 10)
        cls.analysis = analyzer.analyze(list(shakedown) + list(main))
        cls.analysis_path = os.path.join(cls.tmp, "analysis.json")
        with open(cls.analysis_path, "w", encoding="utf-8") as f:
            json.dump(cls.analysis, f, indent=2, sort_keys=True)
            f.write("\n")
        cls.table = support.render_results_table(cls.analysis)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _check(self, table_text):
        """Write a table copy, run check_summary over it with the
        (possibly rewritten) analysis, and return the completed process."""
        md_path = os.path.join(self.tmp, "reproduction-results.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# Reproduction results\n\n" + table_text)
        return support.run_check_summary(md_path, self.analysis_path)

    def _check_analysis(self, analysis):
        analysis_path = os.path.join(self.tmp, "analysis-alt.json")
        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, sort_keys=True)
            f.write("\n")
        md_path = os.path.join(self.tmp, "reproduction-results.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# Reproduction results\n\n" + self.table)
        return support.run_check_summary(md_path, analysis_path)

    def test_help_exits_zero(self):
        result = support.run_check_summary("--help", "--help")
        self.assertEqual(result.returncode, 0)

    def test_accepts_table_matching_analysis(self):
        result = self._check(self.table)
        self.assertEqual(
            result.returncode, 0,
            "check_summary rejected a table that matches the analysis:\n%s"
            % result.stderr.decode(errors="replace"))

    def _assert_rejected(self, table_text, analysis=None):
        if analysis is None:
            result = self._check(table_text)
        else:
            result = self._check_analysis(analysis)
        self.assertNotEqual(
            result.returncode, 0,
            "check_summary accepted a defective table/analysis pair")

    def test_rejects_registration_count_off_by_one(self):
        self._assert_rejected(
            self.table.replace("| 4 | 900 |", "| 5 | 900 |"))

    def test_rejects_index_reuse_mismatch(self):
        # main row's Index reuse cell (2) bumped to 3
        row = [line for line in self.table.splitlines()
               if line.startswith("| synthetic-main ")][0]
        cells = row.split("|")
        cells[12] = " 3 "
        self._assert_rejected(
            self.table.replace(row, "|".join(cells)))

    def test_rejects_full_key_recurrence_mismatch(self):
        row = [line for line in self.table.splitlines()
               if line.startswith("| synthetic-main ")][0]
        cells = row.split("|")
        cells[13] = " 2 "
        self._assert_rejected(
            self.table.replace(row, "|".join(cells)))

    def test_rejects_stop_reason_mismatch(self):
        self._assert_rejected(
            self.table.replace("| count-budget | PASS |",
                               "| explicit-stop | PASS |"))

    def test_rejects_primary_marked_auxiliary(self):
        row = [line for line in self.table.splitlines()
               if line.startswith("| synthetic-main ")][0]
        self._assert_rejected(
            self.table.replace(row,
                               row.replace("| primary |",
                                           "| auxiliary |")))

    def test_rejects_missing_primary_row(self):
        lines = [line for line in self.table.splitlines()
                 if not line.startswith("| synthetic-main ")]
        self._assert_rejected("\n".join(lines) + "\n")

    def test_rejects_unknown_role_marking(self):
        self._assert_rejected(
            self.table.replace("| auxiliary |", "| pilot |"))

    def test_rejects_missing_required_column(self):
        lines = []
        for line in self.table.splitlines():
            if line.startswith("| Run | Role |"):
                header = line.split("|")
                # drop the "Index reuse" column from header and rows
                del header[12]
                line = "|".join(header)
            elif line.startswith("|") and not line.startswith("|--"):
                cells = line.split("|")
                if len(cells) > 12:
                    del cells[12]
                line = "|".join(cells)
            lines.append(line)
        self._assert_rejected("\n".join(lines) + "\n")

    def test_rejects_malformed_source_hash(self):
        self._assert_rejected(
            self.table.replace("a" * 64, "not-a-hash"))

    def test_rejects_over_cap_reg_limit(self):
        row = [line for line in self.table.splitlines()
               if line.startswith("| synthetic-main ")][0]
        self._assert_rejected(
            self.table.replace(row, row.replace("| 600000 |",
                                                "| 600001 |")))

    def test_rejects_wrong_sample_outcome_counts(self):
        main_run = [r for r in self.analysis["runs"]
                    if r["run_role"] == "main"][0]
        counts = support.sample_outcome_counts(main_run)
        correct = " ".join("%s=%d" % (name, counts[name])
                           for name in ("accepted", "rejected",
                                        "indeterminate", "anomalous"))
        wrong = dict(counts)
        wrong["accepted"] += 1
        wrong["rejected"] = max(0, wrong["rejected"] - 1)
        wrong_text = " ".join("%s=%d" % (name, wrong[name])
                              for name in ("accepted", "rejected",
                                           "indeterminate", "anomalous"))
        self.assertIn(correct, self.table,
                      "fixture premise: the rendered main row carries %r"
                      % correct)
        self._assert_rejected(self.table.replace(correct, wrong_text))

    def test_rejects_empty_limitations_cell(self):
        row = [line for line in self.table.splitlines()
               if line.startswith("| synthetic-shakedown ")][0]
        cells = row.split("|")
        self.assertTrue(cells[-2].strip(),
                        "fixture premise: limitations cell non-empty")
        cells[-2] = "  "
        self._assert_rejected(self.table.replace(row, "|".join(cells)))

    def test_rejects_label_mismatch(self):
        self._assert_rejected(
            self.table.replace("| synthetic-main |",
                               "| synthetic-main-renamed |"))

    def test_rejects_analysis_over_aggregate_budget(self):
        analysis = json.loads(json.dumps(self.analysis))
        analysis["aggregate"]["within_registration_budget"] = False
        self._assert_rejected(self.table, analysis=analysis)

    def test_rejects_table_regs_disagreeing_with_aggregate(self):
        # table reg total no longer equals the analysis aggregate total
        analysis = json.loads(json.dumps(self.analysis))
        analysis["aggregate"]["target_registrations_succeeded_total"] = 9
        self._assert_rejected(self.table, analysis=analysis)

    def test_rejects_wall_time_disagreement(self):
        row = [line for line in self.table.splitlines()
               if line.startswith("| synthetic-main ")][0]
        self._assert_rejected(
            self.table.replace(row, row.replace("| 0.0 |", "| 123.4 |")))


class CheckSummaryDatasetTests(unittest.TestCase):
    """The committed docs/reproduction-results.md must agree with the
    archived results/analysis.json (fails loudly until both exist)."""

    def test_committed_table_agrees_with_archived_analysis(self):
        self.assertTrue(os.path.isfile(support.RESULTS_TABLE_PATH),
                        "docs/reproduction-results.md is missing")
        self.assertTrue(os.path.isfile(support.ANALYSIS_PATH),
                        "results/analysis.json is missing")
        result = support.run_check_summary(support.RESULTS_TABLE_PATH,
                                           support.ANALYSIS_PATH)
        self.assertEqual(
            result.returncode, 0,
            "the committed results table disagrees with the archived "
            "analysis:\n%s" % result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
