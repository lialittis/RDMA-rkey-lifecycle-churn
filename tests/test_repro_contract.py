"""Acceptance tests for the reproduction-validation revision contract:

- the version-1 binary ledger format (MAGIC "RKEYCHN1", VERSION 1 and the
  8-byte run_role field) is preserved with NO schema migration -- the
  internal diagnostic role "smoke" is chosen precisely because it fits;
- the frozen original-run tag and its pilot-requiring verifier stay
  preserved while the working tree accepts a main-only primary result;
- the launcher enforces the authorized per-role registration caps and no
  longer offers the pilot role;
- make_manifest.py consumes the shared lab config and emits a
  smoke+main churn-manifest/2 that verify.py accepts, and still emits a
  valid manifest when the optional diagnostic run is absent;
- docs/reproduction-results.md exists with primary/auxiliary markings and
  user-facing "shakedown" naming for the auxiliary run.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import support


class LedgerFormatPreservationTests(unittest.TestCase):
    """The version-1 binary ledger must stay byte-compatible: no widened
    run_role field, no version bump, no schema migration."""

    def test_ledger_magic_and_version_unchanged(self):
        import ledger
        self.assertEqual(ledger.MAGIC, support.LEDGER_V1_MAGIC)
        self.assertEqual(ledger.VERSION, support.LEDGER_V1_VERSION)

    def test_run_role_field_stays_eight_bytes(self):
        import ledger
        # "smoke" and "main" (and the frozen revision's "pilot") fit the
        # 8-byte run_role field and must remain writable...
        with tempfile.TemporaryDirectory() as tmp:
            for role in ("smoke", "main", "pilot"):
                path = os.path.join(tmp, "%s.ledger" % role)
                with ledger.LedgerWriter(path) as w:
                    w.append({"kind": "run-meta", "run_role": role,
                              "label": "x", "endpoint": "owner",
                              "mono_ns": 1})
                    w.checkpoint()
                records = ledger.read_ledger(path).records
                self.assertEqual(records[0]["run_role"], role)
        # ...while a 9-byte role string must still be rejected: the field
        # has NOT been widened for this revision.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wide.ledger")
            with self.assertRaises(ValueError):
                with ledger.LedgerWriter(path) as w:
                    w.append({"kind": "run-meta", "run_role": "shakedown",
                              "label": "x", "endpoint": "owner",
                              "mono_ns": 1})


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
    """scripts/run.py: only the smoke/main roles, per-role caps, no
    network activity at import or during cap validation.  The internal
    diagnostic role is "smoke"; "shakedown" is the user-facing label."""

    def test_run_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, os.path.join(support.REPO_ROOT, "scripts",
                                          "run.py"), "--help"],
            cwd=support.REPO_ROOT, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0)

    def test_help_offers_only_smoke_and_main_roles(self):
        result = subprocess.run(
            [sys.executable, os.path.join(support.REPO_ROOT, "scripts",
                                          "run.py"), "--help"],
            cwd=support.REPO_ROOT, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = result.stdout.decode(errors="replace")
        self.assertNotIn("pilot", text,
                         "the launcher must not offer the pilot role; the "
                         "internal diagnostic role is smoke")
        for role in ("smoke", "main"):
            self.assertIn(role, text)

    def test_role_registration_caps(self):
        run = support.load_script("run_contract", "run.py")
        self.assertEqual(
            getattr(run, "ROLE_MAX_REGS", None),
            {"smoke": support.BUDGETS["smoke_max_target_registrations"],
             "main": support.BUDGETS["main_max_target_registrations"]},
            "run.py must pin the authorized per-role registration caps")
        for role, bad in (("smoke", 4097), ("main", 600001),
                          ("smoke", -1), ("main", 0)):
            with self.assertRaises(
                    (SystemExit, ValueError),
                    msg="%r cap violation %r must be rejected"
                        % (role, bad)):
                run.validate_launch(role, bad, 60)
        # exact caps are legal
        run.validate_launch("smoke", 4096, 60)
        run.validate_launch("main", 600000, 900)

    def test_posthoc_diagnostics_covers_smoke_directories(self):
        posthoc = support.load_script("posthoc_contract",
                                      "posthoc_diagnostics.py")
        prefixes = getattr(posthoc, "RUN_DIR_PREFIXES", None)
        self.assertIn("smoke-", prefixes,
                      "posthoc diagnostics must also cover smoke run "
                      "directories, not only pilot-/main-")
        self.assertIn("main-", prefixes)

    def test_make_manifest_role_order_covers_smoke(self):
        make_manifest = support.load_script("manifest_contract",
                                            "make_manifest.py")
        roles = getattr(make_manifest, "RUN_ROLES", None)
        self.assertEqual(roles, ("smoke", "main"),
                         "make_manifest must index smoke and main run "
                         "directories in this order")


class MakeManifestContractTests(unittest.TestCase):
    """make_manifest.py must turn a results/ tree with one smoke and one
    main run into a churn-manifest/2 that verify.py --strict accepts,
    using the same shared lab config everywhere -- and must still emit a
    valid main-only manifest when the optional diagnostic run is absent."""

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
        return rc

    def _verify_generated(self, root):
        manifest_path = os.path.join(root, "run-manifest.json")
        result = support.run_verify(manifest_path, strict=True)
        self.assertEqual(
            result.returncode, 0,
            "verify.py rejected the freshly generated manifest:\n%s\n%s"
            % (result.stdout.decode(errors="replace"),
               result.stderr.decode(errors="replace")))
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_emits_smoke_main_manifest_that_verifies(self):
        rc = self._run_make_manifest(self.root)
        self.assertEqual(rc, 0, "make_manifest failed on a complete "
                                "smoke+main results tree")
        manifest = self._verify_generated(self.root)
        self.assertEqual(manifest.get("schema"), support.MANIFEST_SCHEMA)
        self.assertEqual([r["role"] for r in manifest["runs"]],
                         self.expected["roles"],
                         "the manifest must carry the smoke auxiliary "
                         "(user-facing shakedown) and the main primary "
                         "run, in order")
        self.assertEqual(manifest.get("budgets"), self.expected["budgets"])
        roles_present = {a["role"] for a in manifest["artifacts"]}
        self.assertIn("results-table", roles_present,
                      "the manifest must index docs/reproduction-results."
                      "md as the results-table artifact")

    def test_main_only_tree_also_emits_valid_manifest(self):
        # The diagnostic run is optional: preflight or safety checks may
        # prevent it, and the manifest must still verify without it.
        root = os.path.join(self.tmp, "main-only")
        shutil.copytree(self.root, root)
        os.remove(os.path.join(root, "run-manifest.json"))
        shutil.rmtree(os.path.join(root, "results",
                                   self.expected["run_dirs"][0]))
        rc = self._run_make_manifest(root)
        self.assertEqual(rc, 0)
        manifest = self._verify_generated(root)
        self.assertEqual([r["role"] for r in manifest["runs"]], ["main"])
        self.assertFalse([a for a in manifest["artifacts"]
                          if a.get("run") == "smoke"],
                         "no smoke-scoped artifacts may remain when the "
                         "diagnostic run is absent")

    def test_fails_loudly_without_a_main_run(self):
        root = os.path.join(self.tmp, "no-main")
        shutil.copytree(self.root, root)
        os.remove(os.path.join(root, "run-manifest.json"))
        shutil.rmtree(os.path.join(root, "results",
                                   self.expected["run_dirs"][-1]))
        with self.assertRaises(SystemExit,
                               msg="make_manifest must fail loudly when "
                                   "no main run exists and no attempt "
                                   "explanation is preserved"):
            self._run_make_manifest(root)


class ResultsTableDocTests(unittest.TestCase):
    """docs/reproduction-results.md must exist as a compact English table
    marking the main run primary and the auxiliary run (internal role
    smoke) with its user-facing name "shakedown" (fails loudly until the
    reproduction delivers it)."""

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
        if "auxiliary" in lowered or support.AUXILIARY_ROLE in lowered:
            # an auxiliary row exists: it must use the user-facing name
            self.assertIn(support.AUXILIARY_USER_FACING, lowered,
                          "the diagnostic run's user-facing name is "
                          "\"shakedown\"; the internal role name must "
                          "not leak into user-facing docs")
        self.assertNotIn("pilot", lowered,
                         "the auxiliary run must never be called a pilot")


if __name__ == "__main__":
    unittest.main()
