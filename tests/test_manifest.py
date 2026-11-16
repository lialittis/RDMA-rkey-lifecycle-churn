"""Acceptance tests for run-manifest.json (structure, pinned values,
artifacts, runs).  See tests/support.py for the full contract."""

import os
import unittest

import support
from support import (MANIFEST_SCHEMA, OWNER_ENDPOINT, REQUESTER_ENDPOINT,
                     REMOTE_DIRECTORY, ALLOCATION_BYTES, PROBE_SPEC,
                     REQUIRED_ACCESS_FLAGS, BUDGETS, REQUIRED_PROHIBITIONS,
                     SAMPLE_PLAN, STOP_REASONS,
                     STOP_REASONS_REQUIRING_EXPLANATION,
                     REQUIRED_GLOBAL_ROLES, REQUIRED_RUN_ROLES)


class ManifestStructureTests(unittest.TestCase):

    def setUp(self):
        self.manifest = support.load_manifest()
        self.manifest_dir = support.REPO_ROOT

    def test_schema_declared(self):
        self.assertEqual(self.manifest.get("schema"), MANIFEST_SCHEMA)

    def test_endpoints_and_remote_directory(self):
        experiment = self.manifest.get("experiment")
        self.assertIsInstance(experiment, dict)
        self.assertEqual(experiment.get("owner_endpoint"), OWNER_ENDPOINT)
        self.assertEqual(experiment.get("requester_endpoint"),
                         REQUESTER_ENDPOINT)
        self.assertEqual(experiment.get("remote_directory"),
                         REMOTE_DIRECTORY)

    def test_config_fixed_allocation_and_probe(self):
        config = self.manifest.get("config")
        self.assertIsInstance(config, dict)
        self.assertEqual(config.get("allocation_bytes"), ALLOCATION_BYTES)
        self.assertEqual(config.get("probe"), PROBE_SPEC)
        access = config.get("mr_access")
        self.assertIsInstance(access, list)
        self.assertTrue(REQUIRED_ACCESS_FLAGS.issubset(set(access)),
                        "mr_access must include LOCAL_WRITE and "
                        "REMOTE_WRITE, got %r" % (access,))
        for flag in ("fixed_allocation", "allocation_alive_until_qps_destroyed",
                     "quiesce_before_deregistration"):
            self.assertIs(config.get(flag), True,
                          "config.%s must be true" % flag)

    def test_budgets_exact(self):
        self.assertEqual(self.manifest.get("budgets"), BUDGETS)

    def test_prohibitions_declared(self):
        prohibitions = self.manifest.get("prohibitions")
        self.assertIsInstance(prohibitions, list)
        self.assertTrue(REQUIRED_PROHIBITIONS.issubset(set(prohibitions)),
                        "prohibitions must include %r, got %r"
                        % (sorted(REQUIRED_PROHIBITIONS), prohibitions))

    def test_sample_plan_predeclared(self):
        plan = self.manifest.get("sample_plan")
        self.assertEqual(plan, SAMPLE_PLAN,
                         "sample plan must predeclare 4 index-only and "
                         "8 full-key samples with no positive stopping "
                         "rule")


class ManifestArtifactTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manifest = support.load_manifest()
        cls.manifest_dir = support.REPO_ROOT

    def test_artifacts_well_formed(self):
        artifacts = support.manifest_artifacts(self.manifest)
        self.assertGreater(len(artifacts), 0)
        seen_paths = set()
        for art in artifacts:
            for key in ("role", "path", "sha256", "bytes"):
                self.assertIn(key, art, "artifact missing %r: %r"
                              % (key, art))
            path = art["path"]
            self.assertIsInstance(path, str)
            self.assertFalse(os.path.isabs(path),
                             "artifact path must be relative: %r" % path)
            self.assertNotIn("..", path.split(os.sep),
                             "artifact path must not escape the repo: %r"
                             % path)
            self.assertNotIn(path, seen_paths,
                             "duplicate artifact path %r" % path)
            seen_paths.add(path)
            self.assertTrue(support.is_sha256_hex(art["sha256"]),
                            "sha256 must be 64 lowercase hex: %r"
                            % art["sha256"])
            self.assertIsInstance(art["bytes"], int)
            self.assertGreaterEqual(art["bytes"], 0)

    def test_artifacts_exist_and_hash_match(self):
        artifacts = support.manifest_artifacts(self.manifest)
        for art in artifacts:
            fs_path = support.artifact_fs_path(self.manifest_dir, art)
            self.assertTrue(os.path.isfile(fs_path),
                            "artifact file missing: %r" % art["path"])
            size = os.path.getsize(fs_path)
            self.assertEqual(size, art["bytes"],
                             "artifact %r size %d != manifest bytes %d"
                             % (art["path"], size, art["bytes"]))
            digest = support.sha256_file(fs_path)
            self.assertEqual(digest, art["sha256"],
                             "artifact %r sha256 mismatch" % art["path"])

    def test_required_global_roles_present(self):
        roles = support.all_roles_present(self.manifest)
        for role in REQUIRED_GLOBAL_ROLES:
            self.assertIn(role, roles,
                          "missing global artifact role %r" % role)
        sources = [a for a in support.manifest_artifacts(self.manifest)
                   if a["role"] == "harness-source"]
        self.assertTrue(any(a["path"].endswith(".c") for a in sources),
                        "at least one harness-source artifact must be C "
                        "source (.c)")
        binaries = [a for a in support.manifest_artifacts(self.manifest)
                    if a["role"] == "binary"]
        self.assertGreaterEqual(len(binaries), 1)
        for art in binaries:
            self.assertGreater(art["bytes"], 0, "binary artifact empty")

    def test_runs_structure(self):
        runs = support.run_entries(self.manifest)
        roles = [r.get("role") for r in runs]
        self.assertEqual(roles.count("pilot"), 1,
                         "exactly one pilot run required, got %r"
                         % (roles,))
        self.assertLessEqual(roles.count("main"), 1)
        for run in runs:
            self.assertIn(run.get("role"), ("pilot", "main"))
            self.assertIsInstance(run.get("label"), str)
            self.assertTrue(run["label"].strip())
            self.assertIn(run.get("stop_reason"), STOP_REASONS,
                          "run %r has invalid stop_reason %r"
                          % (run.get("label"), run.get("stop_reason")))
            # per-run artifact roles
            for role in REQUIRED_RUN_ROLES:
                found = support.artifacts_by_role(self.manifest, role,
                                                  run=run["role"])
                self.assertTrue(found,
                                "run %r missing artifact role %r"
                                % (run["role"], role))
            # stop-record artifact agrees with the manifest stop reason
            stop_arts = support.artifacts_by_role(self.manifest,
                                                  "stop-record",
                                                  run=run["role"])
            self.assertEqual(len(stop_arts), 1)
            stop_path = support.artifact_fs_path(self.manifest_dir,
                                                 stop_arts[0])
            stop_record = support.load_json(stop_path)
            self.assertEqual(stop_record.get("stop_reason"),
                             run["stop_reason"],
                             "stop-record disagrees with manifest run entry")
            self.assertIsInstance(stop_record.get("registrations_at_stop"),
                                  int)
            self.assertIsInstance(stop_record.get("wall_seconds_at_stop"),
                                  (int, float))
            # run-config caps must stay within the global budgets
            cfg_arts = support.artifacts_by_role(self.manifest, "run-config",
                                                 run=run["role"])
            self.assertEqual(len(cfg_arts), 1)
            run_config = support.load_json(
                support.artifact_fs_path(self.manifest_dir, cfg_arts[0]))
            self.assertLessEqual(
                run_config.get("max_wall_seconds",
                               BUDGETS["max_wall_seconds"]),
                BUDGETS["max_wall_seconds"])
            self.assertLessEqual(
                run_config.get("max_target_registrations",
                               BUDGETS["max_target_registrations"]),
                BUDGETS["max_target_registrations"])
            # exactly one owner/requester ledger per run
            self.assertEqual(len(support.artifacts_by_role(
                self.manifest, "owner-ledger", run=run["role"])), 1)
            self.assertEqual(len(support.artifacts_by_role(
                self.manifest, "requester-ledger", run=run["role"])), 1)

    def test_early_stops_have_failure_explanations(self):
        runs = support.run_entries(self.manifest)
        for run in runs:
            if run["stop_reason"] in STOP_REASONS_REQUIRING_EXPLANATION:
                found = support.artifacts_by_role(
                    self.manifest, "failure-explanation", run=run["role"])
                self.assertTrue(found,
                                "early-stopped run %r must preserve a "
                                "failure-explanation artifact"
                                % run["label"])

    def test_main_run_or_infeasibility_explanation(self):
        runs = support.run_entries(self.manifest)
        if not any(r.get("role") == "main" for r in runs):
            found = [a for a in support.manifest_artifacts(self.manifest)
                     if a["role"] == "main-attempt-explanation"]
            self.assertTrue(found,
                            "no main run: a main-attempt-explanation "
                            "artifact must record why the bounded main "
                            "attempt was not feasible")


if __name__ == "__main__":
    unittest.main()
