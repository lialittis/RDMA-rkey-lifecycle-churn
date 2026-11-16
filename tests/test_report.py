"""Acceptance tests for the English report under results/ and the
semantic content of the provenance artifacts (environments, build log,
remote commands, negative checks)."""

import os
import unittest

import support
from support import REMOTE_DIRECTORY


class ReportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manifest = support.load_manifest()
        arts = support.artifacts_by_role(cls.manifest, "report")
        cls.report_path = support.artifact_fs_path(
            support.REPO_ROOT, arts[0])
        with open(cls.report_path, "rb") as f:
            cls.raw = f.read()

    def _analysis_main_run(self):
        import analyzer
        manifest = self.manifest
        paths = support.all_ledger_paths_in_order(manifest,
                                                  support.REPO_ROOT)
        analysis = analyzer.analyze(paths)
        for run in analysis["runs"]:
            if run["run_role"] == "main":
                return run
        return analysis["runs"][0] if analysis["runs"] else None

    def test_report_exists_and_is_english_text(self):
        self.assertGreaterEqual(len(self.raw), 500,
                                "report is too short to carry the required "
                                "results")
        text = self.raw.decode("utf-8")
        ascii_ratio = sum(1 for c in text if ord(c) < 128) / len(text)
        self.assertGreaterEqual(ascii_ratio, 0.99,
                                "report should be plain English text")

    def test_report_carries_headline_counts_and_classifications(self):
        text = self.raw.decode("utf-8")
        main = self._analysis_main_run()
        self.assertIsNotNone(main, "no run to report on")
        for value in (main["target_registrations_attempted"],
                      main["target_registrations_succeeded"],
                      main["index_recurrences_observed"],
                      main["full_key_recurrences_observed"],
                      len(main["samples"])):
            self.assertIn(str(value), text,
                          "report must state the observed count %d" % value)
        self.assertIn("observed", text.lower(),
                      "results must be phrased as observations")
        self.assertIn("indeterminate", text.lower(),
                      "report must use the indeterminate classification "
                      "vocabulary")

    def test_report_states_stop_reasons_and_limits(self):
        text = self.raw.decode("utf-8")
        for run in support.run_entries(self.manifest):
            self.assertIn(run["stop_reason"], text,
                          "report must state the exact stop reason for "
                          "run %r" % run["label"])
        lowered = text.lower()
        self.assertTrue("allocator" in lowered or "visibility" in lowered,
                        "report must record the device-global allocator "
                        "visibility limit (other device MR users)")
        self.assertIn("identical", lowered,
                      "report must note that a recurring full key is "
                      "bit-identical, so a live success resolves to the "
                      "current registration")
        self.assertIn("source", lowered,
                      "report must state that no rate is inferred from "
                      "source alone")


class ProvenanceArtifactTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manifest = support.load_manifest()

    def _load_first(self, role):
        arts = support.artifacts_by_role(self.manifest, role)
        self.assertTrue(arts, "missing artifact role %r" % role)
        return support.load_json(
            support.artifact_fs_path(support.REPO_ROOT, arts[0]))

    def test_environment_artifacts_record_versions(self):
        for role, host in (("environment-owner", support.OWNER_ENDPOINT),
                           ("environment-requester",
                            support.REQUESTER_ENDPOINT)):
            env = self._load_first(role)
            kernel = env.get("kernel_version")
            self.assertIsInstance(kernel, str)
            self.assertTrue(kernel.strip(),
                            "%s must record a kernel version" % role)
            self.assertTrue(env.get("rxe0_active") is True
                            or "rxe" in str(env).lower(),
                            "%s must record the RXE endpoint state" % role)

    def test_build_log_records_command_and_binary_hash(self):
        build = self._load_first("build-log")
        self.assertIsInstance(build.get("command"), str)
        self.assertTrue(build["command"].strip())
        self.assertEqual(build.get("exit_code"), 0,
                         "build must have succeeded")
        self.assertTrue(support.is_sha256_hex(build.get("binary_sha256")))
        binaries = support.artifacts_by_role(self.manifest, "binary")
        binary_hashes = {art["sha256"] for art in binaries}
        self.assertIn(build["binary_sha256"], binary_hashes,
                      "build log hash must match a binary artifact")

    def test_remote_commands_use_dedicated_directory(self):
        commands = self._load_first("remote-commands")
        self.assertIsInstance(commands, list)
        self.assertGreater(len(commands), 0)
        joined = ""
        for entry in commands:
            self.assertIn("host", entry)
            self.assertIn("command", entry)
            self.assertIsInstance(entry["host"], str)
            self.assertIsInstance(entry["command"], str)
            joined += entry["command"]
        self.assertIn(REMOTE_DIRECTORY, joined,
                      "remote commands must operate under the dedicated "
                      "%s directory" % REMOTE_DIRECTORY)

    def test_negative_checks_recorded_and_passing(self):
        checks = self._load_first("negative-checks")
        entries = checks.get("checks", checks)
        self.assertIsInstance(entries, list)
        self.assertGreaterEqual(len(entries), 3,
                                "at least three negative integrity checks "
                                "must be recorded")
        for entry in entries:
            self.assertIs(entry.get("passed"), True,
                          "negative check %r did not pass"
                          % entry.get("check"))


if __name__ == "__main__":
    unittest.main()
