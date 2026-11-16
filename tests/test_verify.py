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
                if not (a["role"] == "owner-ledger" and a.get("run") == "pilot")]
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

    def test_rejects_early_stop_without_explanation(self):
        def mutate(manifest, root):
            for run in manifest["runs"]:
                if run["role"] == "pilot":
                    run["stop_reason"] = "control-failure"
        path = self._mutate_manifest("no-explanation", mutate)
        # The pilot now stopped early with no failure-explanation artifact,
        # and its stop-record still names the old stop reason: both reject.
        self.assertNotEqual(support.run_verify(path).returncode, 0)


if __name__ == "__main__":
    unittest.main()
