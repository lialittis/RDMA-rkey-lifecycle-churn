#!/usr/bin/env python3
"""Reassess historical kernel-diagnostics JSONs from preserved raw data.

The 2026-11-16 launcher ran ``dmesg | tail -120``: the pipeline masked
dmesg's exit status (the shell reported tail's 0) and discarded its
stderr, so an unprivileged kernel-buffer read failure on both VMs looked
like an empty-but-successful capture.  The eight preserved
*-dmesg-before/after.txt files are all 0 bytes, which is the raw-data
signature of that failure; the launcher nonetheless wrote
``"available": true`` into the *-diagnostics.json files.

This script rewrites those historical diagnostics JSONs honestly WITHOUT
touching the preserved raw captures or the historical ledgers: the
original fields are preserved, ``available`` is corrected to false, the
kernel delta is marked unverified, and the reassessment reason is
recorded.  Idempotent.  A sudo-based capture taken after the fact would
be a POST-HOC snapshot, never a historical delta, and is not created
here.

Usage: python3 scripts/reassess_diagnostics.py  (no config, no network)"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MASKED_COMMAND = "dmesg | tail -120"
REASSESSMENT = (
    "2026-11-16 review: the launcher command 'dmesg | tail -120' lost "
    "dmesg's exit status to the pipe and discarded its stderr, so the "
    "unprivileged kernel-buffer read failure was masked; the preserved "
    "before/after capture files alongside this JSON are empty (0 bytes), "
    "which is the raw-data signature of that failure. The historical "
    "kernel delta is therefore unavailable/unverified, and the earlier "
    "'available': true claim is withdrawn. No post-hoc capture was "
    "substituted: a sudo capture taken after the fact is a post-hoc "
    "snapshot, never a historical delta.")


def reassess_file(run_dir, side):
    diag_path = os.path.join(run_dir, "%s-diagnostics.json" % side)
    if not os.path.isfile(diag_path):
        return None
    with open(diag_path, "r", encoding="utf-8") as f:
        diag = json.load(f)
    if diag.get("historical_reassessed"):
        return "already-reassessed"
    if diag.get("command") != MASKED_COMMAND:
        return "not-a-masked-capture"
    captures = []
    for phase in ("before", "after"):
        path = os.path.join(run_dir, "%s-dmesg-%s.txt" % (side, phase))
        captures.append((path, os.path.getsize(path) if os.path.isfile(path)
                         else None))
    if any(size is None or size > 0 for _path, size in captures):
        return "captures-not-empty (no masking signature; left untouched)"
    updated = dict(diag)
    updated.update({
        "available": False,
        "delta_status": "unverified",
        "dmesg_returncode": None,
        "dmesg_stderr": None,
        "new_lines_count": None,
        "new_abnormal_lines": [],
        "historical_reassessed": True,
        "historical_reassessment": REASSESSMENT,
    })
    with open(diag_path, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2)
        f.write("\n")
    return "reassessed"


def main():
    results = os.path.join(REPO, "results")
    outcomes = {}
    for entry in sorted(os.listdir(results)):
        run_dir = os.path.join(results, entry)
        if not (entry.startswith(("smoke-", "main-"))
                and os.path.isdir(run_dir)):
            continue
        for side in ("owner", "requester"):
            outcomes["%s/%s" % (entry, side)] = reassess_file(run_dir, side)
    for key in sorted(outcomes):
        print("%s: %s" % (key, outcomes[key]))
    return 0 if all(v in ("reassessed", "already-reassessed")
                    for v in outcomes.values()) and outcomes else 1


if __name__ == "__main__":
    sys.exit(main())
