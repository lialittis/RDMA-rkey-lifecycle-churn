"""Acceptance tests for the reproduction-validation revision contract:

- the frozen original-run tag and its pilot-requiring verifier stay
  preserved while the working tree accepts a main-only primary result;
- the launcher enforces the authorized per-role registration caps and no
  longer offers the pilot role;
- make_manifest.py consumes the shared lab config and emits a
  shakedown+main churn-manifest/2 that verify.py accepts;
- docs/reproduction-results.md exists with primary/auxiliary markings.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import support


class OriginalRunPreservationTests(unittest.TestCase):
    """The frozen original-run revision (pilot-based, 1048576 budget) must
    remain intact at its git tag while this revision moves on."""

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", support.REPO_ROOT] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def test_original_run_tag_exists_and_resolves(self):
        result = self._git("rev-parse", "original-run")
        self.assertEqual(result.returncode, 0,
                         "the original-run git tag is missing:\n%s"
                         % result.stderr.decode(errors="replace"))
        self.assertTrue(result.stdout.decode().strip())

    def test_frozen_verifier_still_requires_pilot(self):
        result = self._git("show", "original-run:verify.py")
        self.assertEqual(result.returncode, 0)
        text = result.stdout.decode(errors="replace")
        self.assertIn("exactly one pilot run is required", text,
                      "the frozen original-run verifier must keep its "
                      "pilot requirement untouched")
        self.assertIn("1048576", text,
                      "the frozen original-run verifier must keep its "
                      "1048576 registration budget pin")

    def test_working_verifier_accepts_main_only(self):
        with open(os.path.join(support.REPO_ROOT, "verify.py"),
                  "r", encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(
            "exactly one pilot run is required", text,
            "the current verify.py must accept a main-only primary "
            "result (pilot belongs only to the frozen original-run tag)")


class LauncherRoleContractTests(unittest.TestCase):
    """scripts/run.py: only the shakedown/main roles, per-role caps, no
    network activity at import or during cap validation."""

    def test_run_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, os.path.join(support.REPO_ROOT, "scripts",
                                          "run.py"), "--help"],
            cwd=support.REPO_ROOT, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0)

    def test_help_offers_only_shakedown_and_main_roles(self):
        result = subprocess.run(
            [sys.executable, os.path.join(support.REPO_ROOT, "scripts",
                                          "run.py"), "--help"],
            cwd=support.REPO_ROOT, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = result.stdout.decode(errors="replace")
        self.assertNotIn("pilot", text,
                         "the launcher must not offer the pilot role; the "
                         "diagnostic run is the shakedown")
        for role in ("shakedown", "main"):
            self.assertIn(role, text)

    def test_role_registration_caps(self):
        run = support.load_script("run_contract", "run.py")
        self.assertEqual(
            getattr(run, "ROLE_MAX_REGS", None),
            {"shakedown": support.BUDGETS["shakedown_max_target_registrations"],
             "main": support.BUDGETS["main_max_target_registrations"]},
            "run.py must pin the authorized per-role registration caps")
        for role, bad in (("shakedown", 4097), ("main", 600001),
                          ("shakedown", -1), ("main", 0)):
            with self.assertRaises(
                    (SystemExit, ValueError),
                    msg="%r cap violation %r must be rejected"
                        % (role, bad)):
                run.validate_launch(role, bad, 60)
        # exact caps are legal
        run.validate_launch("shakedown", 4096, 60)
        run.validate_launch("main", 600000, 900)

    def test_posthoc_diagnostics_covers_shakedown_directories(self):
        posthoc = support.load_script("posthoc_contract",
                                      "posthoc_diagnostics.py")
        prefixes = getattr(posthoc, "RUN_DIR_PREFIXES", None)
        self.assertIn("shakedown-", prefixes,
                      "posthoc diagnostics must also cover shakedown run "
                      "directories, not only pilot-/main-")
        self.assertIn("main-", prefixes)

    def test_make_manifest_role_order_covers_shakedown(self):
        make_manifest = support.load_script("manifest_contract",
                                            "make_manifest.py")
        roles = getattr(make_manifest, "RUN_ROLES", None)
        self.assertEqual(roles, ("shakedown", "main"),
                         "make_manifest must index shakedown and main run "
                         "directories in this order")


class MakeManifestContractTests(unittest.TestCase):
    """make_manifest.py must turn a results/ tree with one shakedown and
    one main run into a churn-manifest/2 that verify.py --strict accepts,
    using the same shared lab config everywhere."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="churn-mkm-contract-")
        cls.root = os.path.join(cls.tmp, "repo")
        cls.config_path, cls.expected = support.build_results_tree(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run_make_manifest(self, root):
        import contextlib
        import io
        make_manifest = support.load_script("make_manifest_run",
                                            "make_manifest.py")
        make_manifest.REPO = root
        argv = sys.argv
        sys.argv = ["make_manifest.py", "--config", self.config_path]
        try:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = make_manifest.main()
        finally:
            sys.argv = argv
        return rc, stdout.getvalue()

    def test_emits_shakedown_main_manifest_that_verifies(self):
        rc, _stdout = self._run_make_manifest(self.root)
        self.assertEqual(rc, 0, "make_manifest failed on a complete "
                                "shakedown+main results tree")
        manifest_path = os.path.join(self.root, "run-manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest.get("schema"), support.MANIFEST_SCHEMA)
        self.assertEqual([r["role"] for r in manifest["runs"]],
                         self.expected["roles"],
                         "the manifest must carry the shakedown "
                         "auxiliary and the main primary run, in order")
        self.assertEqual(manifest.get("budgets"), self.expected["budgets"])
        roles_present = {a["role"] for a in manifest["artifacts"]}
        self.assertIn("results-table", roles_present,
                      "the manifest must index docs/reproduction-results."
                      "md as the results-table artifact")
        result = support.run_verify(manifest_path, strict=True)
        self.assertEqual(
            result.returncode, 0,
            "verify.py rejected the freshly generated manifest:\n%s\n%s"
            % (result.stdout.decode(errors="replace"),
               result.stderr.decode(errors="replace")))

    def test_fails_loudly_without_a_main_run(self):
        root = os.path.join(self.tmp, "no-main")
        shutil.copytree(self.root, root)
        shutil.rmtree(os.path.join(root, "results",
                                   self.expected["run_dirs"][-1]))
        with self.assertRaises(SystemExit,
                               msg="make_manifest must fail loudly when "
                                   "no main run exists and no attempt "
                                   "explanation is preserved"):
            self._run_make_manifest(root)


class ResultsTableDocTests(unittest.TestCase):
    """docs/reproduction-results.md must exist as a compact English table
    marking the main run primary and the shakedown auxiliary (fails loudly
    until the reproduction delivers it)."""

    def test_results_table_doc_exists_with_markings(self):
        self.assertTrue(os.path.isfile(support.RESULTS_TABLE_PATH),
                        "docs/reproduction-results.md is missing")
        with open(support.RESULTS_TABLE_PATH, "rb") as f:
            raw = f.read()
        self.assertGreaterEqual(len(raw), 300,
                                "the results table is too short to carry "
                                "the required columns and rows")
        text = raw.decode("utf-8")
        ascii_ratio = sum(1 for c in text if ord(c) < 128) / len(text)
        self.assertGreaterEqual(ascii_ratio, 0.95,
                                "the results table should be plain "
                                "English text")
        lowered = text.lower()
        self.assertIn("primary", lowered,
                      "the main run must be marked primary")
        self.assertIn("auxiliary", lowered,
                      "the shakedown run must be marked auxiliary")
        self.assertIn("shakedown", lowered,
                      "the diagnostic shakedown run must be named as such "
                      "(never called a pilot)")


if __name__ == "__main__":
    unittest.main()
