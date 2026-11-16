"""Focused tests for the kernel-diagnostics capture fix in scripts/run.py:
dmesg's real exit status and stderr must be preserved, and a delta may
only be claimed when BOTH before and after captures are valid.  The
historical 'dmesg | tail' form masked unprivileged failures (the 2026-11
review finding); those captures are never valid."""

import unittest

import support


class DiagnosticsCaptureTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.launcher = support.load_script("run_diag_contract",
                                       "run.py")

    def test_diag_command_preserves_dmesg_status_and_stderr(self):
        # no pipeline: the shell must not mask dmesg's exit status
        self.assertNotIn("|", self.launcher.DIAG_CMD)
        self.assertIn("dmesg", self.launcher.DIAG_CMD)
        self.assertIn(self.launcher.DMESG_RC_MARKER, self.launcher.DIAG_CMD)

    def _capture(self, stdout="", stderr="", rc="0"):
        marker_line = self.launcher.DMESG_RC_MARKER + str(rc)
        return self.launcher.split_diag_capture(
            (stdout + "\n" + marker_line).encode(), stderr.encode())

    def test_split_extracts_status_stdout_and_stderr(self):
        cap = self._capture("kmsg one\nkmsg two",
                            "dmesg: read kernel buffer failed", rc="x")
        # a non-integer marker still yields None: never trusted blindly
        self.assertEqual(cap["dmesg_stdout"], "kmsg one\nkmsg two")
        self.assertEqual(cap["dmesg_stderr"],
                         "dmesg: read kernel buffer failed")
        self.assertIsNone(cap["dmesg_returncode"])

    def test_split_integer_marker(self):
        cap = self._capture("kmsg", "", rc="1")
        self.assertEqual(cap["dmesg_returncode"], 1)
        self.assertFalse(self.launcher.capture_is_valid(cap))

    def test_markerless_capture_is_never_valid(self):
        # the historical masked form produced empty stdout with no marker
        cap = self.launcher.split_diag_capture(b"", b"")
        self.assertIsNone(cap["dmesg_returncode"])
        self.assertFalse(self.launcher.capture_is_valid(cap))

    def test_valid_capture(self):
        cap = self._capture("kmsg")
        self.assertEqual(cap["dmesg_returncode"], 0)
        self.assertEqual(cap["dmesg_stderr"], "")
        self.assertTrue(self.launcher.capture_is_valid(cap))

    def test_delta_unverified_when_either_capture_invalid(self):
        good = self._capture("kmsg a")
        # historical masked capture (no marker)
        masked = self.launcher.split_diag_capture(b"", b"")
        for before, after in ((masked, good), (good, masked),
                              (masked, masked)):
            diag = self.launcher.diagnostics_delta(before, after)
            self.assertEqual(diag["delta_status"], "unverified")
            self.assertEqual(diag["new_abnormal_lines"], [])
            self.assertIsNone(diag["new_lines_count"])

    def test_delta_unverified_on_unprivileged_failure(self):
        # dmesg ran but failed: nonzero status and stderr preserved
        failed = self.launcher.split_diag_capture(
            b"__DMESG_RC__=1",
            b"dmesg: read kernel buffer failed: Operation not permitted")
        ok = self._capture("kmsg")
        for before, after in ((ok, failed), (failed, ok)):
            self.assertEqual(
                self.launcher.diagnostics_delta(before, after)["delta_status"],
                "unverified")

    def test_delta_clean_and_abnormal_when_both_valid(self):
        before = self._capture("kmsg a\nkmsg b")
        after_clean = self._capture("kmsg a\nkmsg b\nkmsg c")
        diag = self.launcher.diagnostics_delta(before, after_clean)
        self.assertEqual(diag["delta_status"], "clean")
        self.assertEqual(diag["new_lines_count"], 1)
        after_bad = self._capture("kmsg a\nkmsg b\nBUG: something")
        diag = self.launcher.diagnostics_delta(before, after_bad)
        self.assertEqual(diag["delta_status"], "abnormal-lines")
        self.assertEqual(diag["new_abnormal_lines"], ["BUG: something"])


if __name__ == "__main__":
    unittest.main()
