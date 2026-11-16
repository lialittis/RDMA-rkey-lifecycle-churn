#!/usr/bin/env python3
"""Assemble run-manifest.json for the churn experiment.

Indexes every preserved artifact (sources, build provenance, binaries,
environments, remote commands, schema doc, per-run ledgers/logs/
diagnostics/configs/stops, analysis, report, negative checks) with sizes
and SHA-256 hashes.  Run after results/report.md and results/analysis.json
are final; then `python3 verify.py --strict run-manifest.json` must pass."""

import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OWNER = "192.168.122.112"
REQUESTER = "192.168.122.111"
REMOTE = "/tmp/rkey-lifecycle-churn-glm"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def art(role, path, run=None):
    fs_path = os.path.join(REPO, path)
    entry = {"role": role, "path": path,
             "sha256": sha256_file(fs_path),
             "bytes": os.path.getsize(fs_path)}
    if run:
        entry["run"] = run
    return entry


def main():
    results = os.path.join(REPO, "results")
    runs = []
    artifacts = []

    artifacts.append(art("harness-source", "src/churn.c"))
    artifacts.append(art("harness-source", "scripts/deploy.py"))
    artifacts.append(art("harness-source", "scripts/run.py"))
    artifacts.append(art("harness-source", "scripts/negative_checks.py"))
    artifacts.append(art("harness-source", "scripts/make_manifest.py"))
    artifacts.append(art("harness-source", "ledger.py"))
    artifacts.append(art("harness-source", "analyzer.py"))
    artifacts.append(art("harness-source", "verify.py"))
    artifacts.append(art("ledger-schema", "docs/ledger-schema.md"))
    artifacts.append(art("build-log", "results/build.json"))
    artifacts.append(art("binary", "results/churn.aarch64"))
    artifacts.append(art("environment-owner",
                         "results/environment-owner.json"))
    artifacts.append(art("environment-requester",
                         "results/environment-requester.json"))
    artifacts.append(art("remote-commands", "results/remote-commands.json"))
    artifacts.append(art("report", "results/report.md"))
    artifacts.append(art("analysis", "results/analysis.json"))
    artifacts.append(art("negative-checks", "results/negative-checks.json"))

    run_dirs = []
    for role in ("pilot", "main"):
        candidates = sorted(d for d in os.listdir(results)
                            if d.startswith(role + "-")
                            and os.path.isdir(os.path.join(results, d)))
        if candidates:
            run_dirs.append((role, candidates[-1]))
        elif role == "main":
            artifacts.append(art("main-attempt-explanation",
                                 "results/main-attempt-explanation.md"))

    for role, dirname in run_dirs:
        run_dir = os.path.join(results, dirname)
        stop = json.load(open(os.path.join(run_dir, "stop-record.json")))
        runs.append({"role": role, "label": dirname,
                     "stop_reason": stop["stop_reason"]})
        rel = lambda name: "results/%s/%s" % (dirname, name)  # noqa: E731
        artifacts.append(art("owner-ledger", rel("owner.ledger"), run=role))
        artifacts.append(art("requester-ledger", rel("requester.ledger"),
                             run=role))
        artifacts.append(art("raw-logs", rel("owner.log"), run=role))
        artifacts.append(art("raw-logs", rel("requester.log"), run=role))
        artifacts.append(art("raw-logs", rel("owner.err"), run=role))
        artifacts.append(art("raw-logs", rel("requester.err"), run=role))
        artifacts.append(art("diagnostics", rel("owner-diagnostics.json"),
                             run=role))
        artifacts.append(art("diagnostics",
                             rel("requester-diagnostics.json"), run=role))
        artifacts.append(art("run-config", rel("run-config.json"), run=role))
        artifacts.append(art("stop-record", rel("stop-record.json"), run=role))
        artifacts.append(art("raw-logs", rel("execution.json"), run=role))
        artifacts.append(art("raw-logs", rel("owner-env-before.json"),
                             run=role))
        artifacts.append(art("raw-logs", rel("owner-env-after.json"),
                             run=role))
        artifacts.append(art("raw-logs", rel("requester-env-before.json"),
                             run=role))
        artifacts.append(art("raw-logs", rel("requester-env-after.json"),
                             run=role))
        if stop["stop_reason"] in ("control-failure", "cleanup-warning",
                                   "memory-mismatch", "probe-timeout"):
            artifacts.append(art("failure-explanation",
                                 rel("failure-explanation.md"), run=role))

    manifest = {
        "schema": "churn-manifest/1",
        "experiment": {
            "name": "bounded-rxe-registration-churn",
            "owner_endpoint": OWNER,
            "requester_endpoint": REQUESTER,
            "remote_directory": REMOTE,
        },
        "config": {
            "allocation_bytes": 4096,
            "probe": {"opcode": "RDMA_WRITE", "transport": "RC",
                      "length": 64, "offset": 64, "nonzero": True},
            "mr_access": ["LOCAL_WRITE", "REMOTE_WRITE"],
            "fixed_allocation": True,
            "allocation_alive_until_qps_destroyed": True,
            "quiesce_before_deregistration": True,
        },
        "budgets": {"max_wall_seconds": 900,
                    "max_target_registrations": 1048576,
                    "calibration_prefix_registrations": 4096},
        "prohibitions": ["device-reset", "index-range-restriction",
                         "key-injection", "failed-probe-retry",
                         "synthetic-recurrence-as-observation"],
        "sample_plan": {"index_only": 4, "full_key": 8,
                        "positive_stopping_rule": False},
        "runs": runs,
        "artifacts": artifacts,
    }
    out = os.path.join(REPO, "run-manifest.json")
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps({"manifest": out, "runs": runs,
                      "artifacts": len(artifacts)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
