#!/usr/bin/env python3
"""Negative integrity checks over the archived run ledgers.

For every run in results/ with an owner.ledger: flip one byte in a copy
and require ledger verification and the analyzer CLI to reject it; build a
temporary manifest with one wrong hash and require verify.py to reject it.
Writes results/negative-checks.json.  Only copies are mutated; archived
measurements are never modified."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import ledger  # noqa: E402


def main():
    results_dir = os.path.join(REPO, "results")
    checks = []

    def record(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed),
                       "detail": detail})

    ledgers = []
    for entry in sorted(os.listdir(results_dir)):
        run_dir = os.path.join(results_dir, entry)
        if os.path.isdir(run_dir) and os.path.isfile(os.path.join(run_dir, "owner.ledger")):
            ledgers.append(os.path.join(run_dir, "owner.ledger"))
    if not ledgers:
        print("no archived ledgers found")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        for src in ledgers:
            name = os.path.basename(os.path.dirname(src))
            copy = os.path.join(tmp, "%s-tampered.ledger" % name)
            shutil.copyfile(src, copy)
            raw = bytearray(open(copy, "rb").read())
            raw[len(raw) // 2] ^= 0x01
            with open(copy, "wb") as f:
                f.write(bytes(raw))
            # 1. ledger verification must reject the tampered copy
            try:
                ledger.read_ledger(copy).verify()
                record("ledger-tamper-rejected:%s" % name, False,
                       "tampered ledger verified (must not happen)")
            except ledger.LedgerIntegrityError as exc:
                record("ledger-tamper-rejected:%s" % name, True, str(exc)[:120])
            # 2. the analyzer CLI must reject the tampered copy
            out = os.path.join(tmp, "out-%s" % name)
            os.makedirs(out, exist_ok=True)
            r = subprocess.run([sys.executable,
                                os.path.join(REPO, "analyzer.py"),
                                "--out", out, copy],
                               capture_output=True, timeout=600)
            record("analyzer-tamper-rejected:%s" % name, r.returncode != 0,
                    (r.stderr.decode(errors="replace") or "")[:120])
            # 3. verify.py must reject a manifest with a wrong artifact hash
            good = os.path.join(tmp, "artifact.bin")
            shutil.copyfile(src, good)
            import hashlib
            digest = hashlib.sha256(open(good, "rb").read()).hexdigest()
            manifest = {
                "schema": "churn-manifest/1",
                "experiment": {
                    "owner_endpoint": "192.168.122.112",
                    "requester_endpoint": "192.168.122.111",
                    "remote_directory": "/tmp/rkey-lifecycle-churn-glm"},
                "config": {"allocation_bytes": 4096,
                           "probe": {"opcode": "RDMA_WRITE", "transport": "RC",
                                     "length": 64, "offset": 64,
                                     "nonzero": True},
                           "mr_access": ["LOCAL_WRITE", "REMOTE_WRITE"],
                           "fixed_allocation": True,
                           "allocation_alive_until_qps_destroyed": True,
                           "quiesce_before_deregistration": True},
                "budgets": {"max_wall_seconds": 900,
                            "max_target_registrations": 1048576,
                            "calibration_prefix_registrations": 4096},
                "prohibitions": ["device-reset", "index-range-restriction",
                                 "key-injection", "failed-probe-retry",
                                 "synthetic-recurrence-as-observation"],
                "sample_plan": {"index_only": 4, "full_key": 8,
                                "positive_stopping_rule": False},
                "runs": [{"role": "pilot", "label": "synthetic",
                          "stop_reason": "count-budget"}],
                "artifacts": [{"role": "harness-source", "path": "artifact.bin",
                               "sha256": ("0" * 63 + "1"),
                               "bytes": os.path.getsize(good)}]}
            mpath = os.path.join(tmp, "run-manifest.json")
            with open(mpath, "w") as f:
                json.dump(manifest, f)
            r = subprocess.run([sys.executable, os.path.join(REPO, "verify.py"),
                                "--strict", mpath], capture_output=True,
                               timeout=120)
            record("verify-bad-hash-rejected:%s" % name, r.returncode != 0,
                   r.stderr.decode(errors="replace")[:200])

    payload = {"checks": checks}
    with open(os.path.join(results_dir, "negative-checks.json"), "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    failed = [c for c in checks if not c["passed"]]
    print(json.dumps({"checks": len(checks), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
