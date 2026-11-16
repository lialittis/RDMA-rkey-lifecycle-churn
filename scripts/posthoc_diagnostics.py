#!/usr/bin/env python3
"""Post-hoc kernel diagnostics for already-archived runs.

The launcher's plain `dmesg` capture failed (unprivileged kernel buffer
read); that unavailability is preserved in the original diagnostics JSON.
Passwordless sudo dmesg works on both VMs, so this script fetches the full
buffer after the fact, saves it per run host, filters RXE-related and
warning lines, and records them as a posthoc section — explicitly labeled
as not a before/after delta."""

import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from lab_config import ConfigError, load_config  # noqa: E402

# Run-directory prefixes this revision processes: the internal diagnostic
# "smoke" auxiliary (user-facing name: shakedown) and the primary main
# run.  The frozen revision's pilot- prefix is intentionally excluded.
RUN_DIR_PREFIXES = ("smoke-", "main-")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="TOML lab config (default: config.local.toml)")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    results = os.path.join(REPO, "results")
    run_dirs = [d for d in sorted(os.listdir(results))
                if d.startswith(RUN_DIR_PREFIXES)
                and os.path.isdir(os.path.join(results, d))]
    for host, name in ((config.owner_ip, "owner"),
                       (config.requester_ip, "requester")):
        r = subprocess.run(config.ssh_args() + [config.target(host), "sudo -n dmesg"],
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
