#!/usr/bin/env python3
"""Post-hoc kernel diagnostics for already-archived runs.

The launcher's plain `dmesg` capture failed (unprivileged kernel buffer
read); that unavailability is preserved in the original diagnostics JSON.
Passwordless sudo dmesg works on both VMs, so this script fetches the full
buffer after the fact, saves it per run host, filters RXE-related and
warning lines, and records them as a posthoc section — explicitly labeled
as not a before/after delta."""

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = "192.168.122.112"
CLIENT = "192.168.122.111"
SSH = ["ssh", "-F", "/dev/null", "-i",
       "/home/tianchi/epochrdma/keys/epochrdma_vm", "-o", "BatchMode=yes",
       "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8"]


def main():
    results = os.path.join(REPO, "results")
    run_dirs = [d for d in sorted(os.listdir(results))
                if (d.startswith("pilot-") or d.startswith("main-"))
                and os.path.isdir(os.path.join(results, d))]
    for host, name in ((OWNER, "owner"), (CLIENT, "requester")):
        r = subprocess.run(SSH + ["ubuntu@" + host, "sudo -n dmesg"],
                           capture_output=True, timeout=60)
        text = r.stdout.decode(errors="replace")
        lines = text.splitlines()
        rxe = [ln for ln in lines if "rxe" in ln.lower()]
        abnormal = [ln for ln in lines if "WARNING" in ln or "BUG" in ln
                    or "oops" in ln.lower()]
        payload = {"source": "sudo -n dmesg (post-hoc, not a delta)",
                   "returncode": r.returncode,
                   "total_lines": len(lines),
                   "rxe_lines": rxe[-40:],
                   "abnormal_lines": abnormal[-40:]}
        for d in run_dirs:
            out_txt = os.path.join(results, d, "%s-dmesg-posthoc.txt" % name)
            with open(out_txt, "w") as f:
                f.write(text)
            diag_path = os.path.join(results, d, "%s-diagnostics.json" % name)
            diag = {"available": r.returncode == 0,
                    "command": "sudo -n dmesg",
                    "posthoc": True, **payload}
            with open(diag_path, "w") as f:
                json.dump(diag, f, indent=2)
    print(json.dumps({"runs_updated": run_dirs}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
