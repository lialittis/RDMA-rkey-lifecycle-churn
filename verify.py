#!/usr/bin/env python3
"""Strict verifier for the churn experiment manifest.

Usage: python3 verify.py [--strict] MANIFEST

Exits 0 only when the manifest is structurally valid, its pinned
experiment configuration matches the declared contract (endpoints, fixed
allocation, probe shape, budgets, prohibitions, sample plan), every listed
artifact exists with matching size and SHA-256, all required artifact
roles are present (global and per-run), and every run carries a valid stop
reason with preserved failure explanations for early stops.  Any
violation is reported on stderr and the exit code is nonzero.

Manifest artifact paths are interpreted relative to the manifest file's
own directory.  --strict is the canonical invocation; the full check set
always runs.
"""

import argparse
import hashlib
import json
import os
import sys

MANIFEST_SCHEMA = "churn-manifest/1"
OWNER_ENDPOINT = "192.168.122.112"
REQUESTER_ENDPOINT = "192.168.122.111"
REMOTE_DIRECTORY = "/tmp/rkey-lifecycle-churn-glm"
ALLOCATION_BYTES = 4096
PROBE_SPEC = {"opcode": "RDMA_WRITE", "transport": "RC",
              "length": 64, "offset": 64, "nonzero": True}
REQUIRED_ACCESS_FLAGS = {"LOCAL_WRITE", "REMOTE_WRITE"}
BUDGETS = {"max_wall_seconds": 900,
           "max_target_registrations": 1048576,
           "calibration_prefix_registrations": 4096}
REQUIRED_PROHIBITIONS = {"device-reset", "index-range-restriction",
                         "key-injection", "failed-probe-retry",
                         "synthetic-recurrence-as-observation"}
SAMPLE_PLAN = {"index_only": 4, "full_key": 8,
               "positive_stopping_rule": False}
STOP_REASONS = {"time-budget", "count-budget", "control-failure",
                "cleanup-warning", "memory-mismatch", "probe-timeout",
                "explicit-stop"}
STOP_REASONS_REQUIRING_EXPLANATION = {"control-failure", "cleanup-warning",
                                      "memory-mismatch", "probe-timeout"}
REQUIRED_GLOBAL_ROLES = {"harness-source", "build-log", "binary",
                         "environment-owner", "environment-requester",
                         "remote-commands", "ledger-schema", "report",
                         "analysis", "negative-checks"}
REQUIRED_RUN_ROLES = {"owner-ledger", "requester-ledger", "raw-logs",
                      "diagnostics", "run-config", "stop-record"}
EXACT_ONE_RUN_ROLES = {"owner-ledger", "requester-ledger", "run-config",
                       "stop-record"}


class Violations(object):

    def __init__(self):
        self.items = []

    def add(self, message):
        self.items.append(message)

    def ok(self):
        return not self.items


def is_sha256_hex(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def check_manifest(manifest, manifest_dir):
    v = Violations()

    if not isinstance(manifest, dict):
        v.add("manifest is not a JSON object")
        return v

    if manifest.get("schema") != MANIFEST_SCHEMA:
        v.add("schema must be %r, got %r"
              % (MANIFEST_SCHEMA, manifest.get("schema")))

    experiment = manifest.get("experiment")
    if not isinstance(experiment, dict):
        v.add("missing 'experiment' object")
    else:
        if experiment.get("owner_endpoint") != OWNER_ENDPOINT:
            v.add("experiment.owner_endpoint must be %r" % OWNER_ENDPOINT)
        if experiment.get("requester_endpoint") != REQUESTER_ENDPOINT:
            v.add("experiment.requester_endpoint must be %r"
                  % REQUESTER_ENDPOINT)
        if experiment.get("remote_directory") != REMOTE_DIRECTORY:
            v.add("experiment.remote_directory must be %r"
                  % REMOTE_DIRECTORY)

    config = manifest.get("config")
    if not isinstance(config, dict):
        v.add("missing 'config' object")
    else:
        if config.get("allocation_bytes") != ALLOCATION_BYTES:
            v.add("config.allocation_bytes must be %d" % ALLOCATION_BYTES)
        if config.get("probe") != PROBE_SPEC:
            v.add("config.probe must be %r, got %r"
                  % (PROBE_SPEC, config.get("probe")))
        access = config.get("mr_access")
        if not isinstance(access, list) or \
                not REQUIRED_ACCESS_FLAGS.issubset(set(access)):
            v.add("config.mr_access must include %r"
                  % sorted(REQUIRED_ACCESS_FLAGS))
        for flag in ("fixed_allocation",
                     "allocation_alive_until_qps_destroyed",
                     "quiesce_before_deregistration"):
            if config.get(flag) is not True:
                v.add("config.%s must be true" % flag)

    if manifest.get("budgets") != BUDGETS:
        v.add("budgets must be exactly %r, got %r"
              % (BUDGETS, manifest.get("budgets")))

    prohibitions = manifest.get("prohibitions")
    if not isinstance(prohibitions, list) or \
            not REQUIRED_PROHIBITIONS.issubset(set(prohibitions)):
        v.add("prohibitions must include %r" % sorted(REQUIRED_PROHIBITIONS))

    if manifest.get("sample_plan") != SAMPLE_PLAN:
        v.add("sample_plan must be exactly %r, got %r"
              % (SAMPLE_PLAN, manifest.get("sample_plan")))

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        v.add("'artifacts' must be a non-empty list")
        artifacts = []

    by_role = {}
    paths_seen = set()
    for art in artifacts:
        if not isinstance(art, dict):
            v.add("artifact entry is not an object: %r" % (art,))
            continue
        for key in ("role", "path", "sha256", "bytes"):
            if key not in art:
                v.add("artifact missing %r: %r" % (key, art))
        role = art.get("role")
        path = art.get("path")
        by_role.setdefault((role, art.get("run")), []).append(art)
        if not isinstance(path, str) or not path:
            v.add("artifact path must be a non-empty string: %r" % (art,))
            continue
        if os.path.isabs(path) or ".." in path.split("/"):
            v.add("artifact path must be relative and contained: %r" % path)
            continue
        if path in paths_seen:
            v.add("duplicate artifact path %r" % path)
        paths_seen.add(path)
        if not is_sha256_hex(art.get("sha256")):
            v.add("artifact sha256 must be 64 lowercase hex: %r" % path)
            continue
        if not isinstance(art.get("bytes"), int) or art["bytes"] < 0:
            v.add("artifact bytes must be a non-negative int: %r" % path)
            continue
        fs_path = os.path.join(manifest_dir, path)
        if not os.path.isfile(fs_path):
            v.add("artifact file missing: %r" % path)
            continue
        size = os.path.getsize(fs_path)
        if size != art["bytes"]:
            v.add("artifact %r size %d != manifest bytes %d"
                  % (path, size, art["bytes"]))
            continue
        digest = hashlib.sha256()
        with open(fs_path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != art["sha256"]:
            v.add("artifact %r sha256 mismatch" % path)

    for role in sorted(REQUIRED_GLOBAL_ROLES):
        if not by_role.get((role, None)):
            v.add("missing global artifact role %r" % role)
    sources = by_role.get(("harness-source", None), [])
    if sources and not any(a["path"].endswith(".c") for a in sources):
        v.add("at least one harness-source artifact must be C source (.c)")
    for art in by_role.get(("binary", None), []):
        if art["bytes"] <= 0:
            v.add("binary artifact %r is empty" % art["path"])

    runs = manifest.get("runs")
    if not isinstance(runs, list) or not runs:
        v.add("'runs' must be a non-empty list")
        runs = []
    roles = [r.get("role") for r in runs if isinstance(r, dict)]
    if roles.count("pilot") != 1:
        v.add("exactly one pilot run is required, got %r" % (roles,))
    if roles.count("main") > 1:
        v.add("at most one main run is allowed")
    for run in runs:
        if not isinstance(run, dict):
            v.add("run entry is not an object: %r" % (run,))
            continue
        role = run.get("role")
        if role not in ("pilot", "main"):
            v.add("run role must be pilot/main, got %r" % (role,))
            continue
        label = run.get("label")
        if not isinstance(label, str) or not label.strip():
            v.add("run %r needs a non-empty label" % (role,))
        stop = run.get("stop_reason")
        if stop not in STOP_REASONS:
            v.add("run %r has invalid stop_reason %r" % (role, stop))
            continue
        for req in sorted(REQUIRED_RUN_ROLES):
            found = by_role.get((req, role), [])
            if not found:
                v.add("run %r missing artifact role %r" % (role, req))
            elif req in EXACT_ONE_RUN_ROLES and len(found) != 1:
                v.add("run %r role %r must appear exactly once"
                      % (role, req))
        if stop in STOP_REASONS_REQUIRING_EXPLANATION and \
                not by_role.get(("failure-explanation", role)):
            v.add("early-stopped run %r (%s) needs a failure-explanation "
                  "artifact" % (role, stop))
        stop_arts = by_role.get(("stop-record", role), [])
        if len(stop_arts) == 1:
            fs_path = os.path.join(manifest_dir, stop_arts[0]["path"])
            try:
                with open(fs_path, "r", encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, ValueError) as exc:
                v.add("run %r stop-record unreadable: %s" % (role, exc))
            else:
                if record.get("stop_reason") != stop:
                    v.add("run %r stop-record stop_reason %r != manifest "
                          "%r" % (role, record.get("stop_reason"), stop))
                if not isinstance(record.get("registrations_at_stop"), int):
                    v.add("run %r stop-record registrations_at_stop must "
                          "be an int" % role)
                if not isinstance(record.get("wall_seconds_at_stop"),
                                  (int, float)):
                    v.add("run %r stop-record wall_seconds_at_stop must "
                          "be a number" % role)
        cfg_arts = by_role.get(("run-config", role), [])
        if len(cfg_arts) == 1:
            fs_path = os.path.join(manifest_dir, cfg_arts[0]["path"])
            try:
                with open(fs_path, "r", encoding="utf-8") as f:
                    run_config = json.load(f)
            except (OSError, ValueError) as exc:
                v.add("run %r run-config unreadable: %s" % (role, exc))
            else:
                if run_config.get("max_wall_seconds",
                                  BUDGETS["max_wall_seconds"]) > \
                        BUDGETS["max_wall_seconds"]:
                    v.add("run %r wall cap exceeds the global budget"
                          % role)
                if run_config.get("max_target_registrations",
                                  BUDGETS["max_target_registrations"]) > \
                        BUDGETS["max_target_registrations"]:
                    v.add("run %r registration cap exceeds the global "
                          "budget" % role)

    if "main" not in roles and not by_role.get(
            ("main-attempt-explanation", None)):
        v.add("no main run: a main-attempt-explanation artifact is "
              "required")

    return v


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Strict verifier for churn run-manifest.json")
    parser.add_argument("--strict", action="store_true",
                        help="run the full check set (canonical)")
    parser.add_argument("manifest", help="path to run-manifest.json")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.manifest):
        sys.stderr.write("manifest not found: %s\n" % args.manifest)
        return 2
    try:
        with open(args.manifest, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except ValueError as exc:
        sys.stderr.write("manifest is not valid JSON: %s\n" % exc)
        return 2

    violations = check_manifest(manifest,
                                os.path.dirname(os.path.abspath(
                                    args.manifest)))
    for item in violations.items:
        sys.stderr.write("violation: %s\n" % item)
    if not violations.ok():
        sys.stderr.write("manifest verification FAILED (%d violations)\n"
                         % len(violations.items))
        return 1
    sys.stdout.write("manifest verification OK (%d artifacts)\n"
                     % len(manifest.get("artifacts", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
