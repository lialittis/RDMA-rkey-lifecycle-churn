"""Acceptance tests for verify.py --strict run-manifest.json: it must
accept the real manifest and reject tampered/incomplete ones."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import support


class VerifyRealManifestTests(unittest.TestCase):

    def test_verify_help_exits_zero(self):
        result = subprocess.run(
            ["python3", os.path.join(support.REPO_ROOT, "verify.py"),
             "--help"],
            cwd=support.REPO_ROOT, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0)

    def test_verify_strict_real_manifest_exits_zero(self):
        result = support.run_verify(support.MANIFEST_PATH, strict=True)
        self.assertEqual(result.returncode, 0,
                         "verify.py --strict failed on the real manifest:\n"
                         "%s\n%s" % (result.stdout.decode(),
                                     result.stderr.decode()))

    def test_verify_missing_manifest_exits_nonzero(self):
        result = support.run_verify(
            os.path.join(support.REPO_ROOT, "no-such-manifest.json"))
        self.assertNotEqual(result.returncode, 0)


class VerifySyntheticManifestTests(unittest.TestCase):
    """verify.py must accept a complete miniature manifest and reject
    specific corruptions of it (run on copies, never on repo files)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="churn-verify-synth-")
        cls.manifest_path = support.build_synthetic_manifest_tree(
            os.path.join(cls.tmp, "ok"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _fresh_copy(self, name):
        root = os.path.join(self.tmp, name)
        shutil.copytree(os.path.join(self.tmp, "ok"), root)
        return os.path.join(root, "run-manifest.json")

    def _mutate_manifest(self, name, mutate):
        path = self._fresh_copy(name)
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        mutate(manifest, os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        return path

    def test_accepts_complete_synthetic_manifest(self):
        result = support.run_verify(self.manifest_path, strict=True)
        self.assertEqual(result.returncode, 0,
                         "verify rejected a complete synthetic manifest:\n"
                         "%s" % result.stdout.decode())

    def test_accepts_different_valid_endpoint_config(self):
        def mutate(manifest, root):
            manifest["experiment"].update({
                "owner_endpoint": "198.51.100.12",
                "requester_endpoint": "198.51.100.11",
                "remote_directory": "/tmp/another-rxe-run"})
        path = self._mutate_manifest("other-endpoints", mutate)
        self.assertEqual(support.run_verify(path, strict=True).returncode, 0)

    def test_rejects_invalid_endpoint_config(self):
        def mutate(manifest, root):
            manifest["experiment"]["owner_endpoint"] = "not-an-ip"
        path = self._mutate_manifest("bad-endpoint", mutate)
        self.assertNotEqual(support.run_verify(path, strict=True).returncode, 0)

    def test_rejects_wrong_artifact_hash(self):
        def mutate(manifest, root):
            manifest["artifacts"][0]["sha256"] = \
                ("0" * 63 + "1")
        path = self._mutate_manifest("bad-hash", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_tampered_artifact_bytes(self):
        def mutate(manifest, root):
            target = None
            for art in manifest["artifacts"]:
                if art["role"] == "report":
                    target = art
                    break
            with open(os.path.join(root, target["path"]), "ab") as f:
                f.write(b"tampered")
        path = self._mutate_manifest("bad-bytes", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_missing_required_role(self):
        def mutate(manifest, root):
            manifest["artifacts"] = [
                a for a in manifest["artifacts"]
                if a["role"] != "environment-owner"]
        path = self._mutate_manifest("missing-role", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_missing_run_artifact_role(self):
        def mutate(manifest, root):
            manifest["artifacts"] = [
                a for a in manifest["artifacts"]
                if not (a["role"] == "owner-ledger"
                        and a.get("run") == "shakedown")]
        path = self._mutate_manifest("missing-run-role", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_invalid_stop_reason(self):
        def mutate(manifest, root):
            manifest["runs"][0]["stop_reason"] = "samples-collected"
        path = self._mutate_manifest("bad-stop", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_tampered_budget(self):
        def mutate(manifest, root):
            manifest["budgets"]["max_wall_seconds"] = 9999
        path = self._mutate_manifest("bad-budget", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_tampered_role_budget(self):
        def mutate(manifest, root):
            manifest["budgets"]["main_max_target_registrations"] = 600001
        path = self._mutate_manifest("bad-role-budget", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_early_stop_without_explanation(self):
        def mutate(manifest, root):
            for run in manifest["runs"]:
                if run["role"] == "shakedown":
                    run["stop_reason"] = "control-failure"
        path = self._mutate_manifest("no-explanation", mutate)
        # The shakedown now stopped early with no failure-explanation
        # artifact, and its stop-record still names the old stop reason:
        # both reject.
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_pilot_role_run(self):
        # The pilot role belongs to the frozen original-run revision; a
        # churn-manifest/2 manifest must not carry it.
        def mutate(manifest, root):
            for run in manifest["runs"]:
                if run["role"] == "shakedown":
                    run["role"] = "pilot"
            for art in manifest["artifacts"]:
                if art.get("run") == "shakedown":
                    art["run"] = "pilot"
        path = self._mutate_manifest("pilot-role", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_missing_main_run_entry(self):
        def mutate(manifest, root):
            manifest["runs"] = [r for r in manifest["runs"]
                                if r["role"] != "main"]
        path = self._mutate_manifest("no-main", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_duplicate_main_runs(self):
        def mutate(manifest, root):
            for run in manifest["runs"]:
                if run["role"] == "shakedown":
                    run["role"] = "main"
            for art in manifest["artifacts"]:
                if art.get("run") == "shakedown":
                    art["run"] = "main"
        path = self._mutate_manifest("two-mains", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_shakedown_over_role_cap(self):
        def mutate(manifest, root):
            for art in manifest["artifacts"]:
                if art.get("role") == "run-config" and \
                        art.get("run") == "shakedown":
                    fs_path = os.path.join(root, art["path"])
                    with open(fs_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    cfg["max_target_registrations"] = 4097
                    with open(fs_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2, sort_keys=True)
                    # keep the manifest hash consistent so ONLY the role
                    # cap check can reject this manifest
                    art["bytes"] = os.path.getsize(fs_path)
                    art["sha256"] = support.sha256_file(fs_path)
        path = self._mutate_manifest("shakedown-over-cap", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)

    def test_rejects_missing_results_table_role(self):
        def mutate(manifest, root):
            manifest["artifacts"] = [
                a for a in manifest["artifacts"]
                if a["role"] != "results-table"]
        path = self._mutate_manifest("no-table", mutate)
        self.assertNotEqual(support.run_verify(path).returncode, 0)


if __name__ == "__main__":
    unittest.main()
