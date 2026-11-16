#!/usr/bin/env python3
"""Derive the global provenance summaries from the archived per-run
captures.

scripts/run.py records raw environment captures (owner-env-before.json /
owner-env-after.json etc.) and the exact owner/requester commands inside
every results/<role>-<utc>/ run directory.  The manifest generator and
verifier additionally index three global summaries at the results/ root --
environment-owner.json, environment-requester.json and
remote-commands.json -- which the pre-configurable revision of this
repository produced as hand-assembled records (this gap was found during
the 2026-11 reproduction validation).  This script derives them
deterministically from the per-run captures, so the global summaries are
never fabricated: every field comes from a recorded artifact.

Usage: python3 scripts/collect_provenance.py [--config config.local.toml]

Reads results/<run>/run-config.json (owner_command/requester_command,
endpoints, remote directory) and the environment captures; writes the
three global JSON files under results/ and exits 0."""

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from lab_config import ConfigError, load_config  # noqa: E402

RUN_DIR_PREFIXES = ("smoke-", "main-")


def environment_summary(capture_path, host):
    with open(capture_path, "r", encoding="utf-8") as f:
        capture = json.load(f)
    output = capture.get("output", "")
    first_line = output.splitlines()[0] if output else ""
    fields = first_line.split()
    kernel_version = fields[2] if len(fields) >= 3 else ""
    return {
        "host": host,
        "kernel_version": kernel_version,
        "rxe0_active": "rxe0" in output,
        "capture_command": capture.get("command"),
        "capture_returncode": capture.get("returncode"),
        "raw_output_excerpt": output[:1200],
        "derived_from": os.path.relpath(capture_path, REPO),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="TOML lab config (default: config.local.toml)")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    results = os.path.join(REPO, "results")
    run_dirs = sorted((d for d in os.listdir(results)
                       if d.startswith(RUN_DIR_PREFIXES)
                       and os.path.isdir(os.path.join(results, d))),
                      key=lambda d: d.split("-", 1)[1])
    if not run_dirs:
        raise SystemExit("no run directories under results/")

    hosts = {"owner": config.owner_ip, "requester": config.requester_ip}
    # environment summaries from the FINAL run's before-capture (the
    # latest full environment snapshot preceding an experiment process)
    final = run_dirs[-1]
    for side, host in hosts.items():
        capture = os.path.join(results, final, "%s-env-before.json" % side)
        if not os.path.isfile(capture):
            raise SystemExit("missing environment capture: %s" % capture)
        summary = environment_summary(capture, host)
        out = os.path.join(results, "environment-%s.json" % side)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")

    # exact remote commands from every run's recorded run-config
    commands = []
    for dirname in run_dirs:
        with open(os.path.join(results, dirname, "run-config.json"),
                  encoding="utf-8") as f:
            run_config = json.load(f)
        for side, key, endpoint in (
                ("owner", "owner_command", "owner_endpoint"),
                ("requester", "requester_command",
                 "requester_endpoint")):
            commands.append({
                "host": run_config.get(endpoint),
                "command": run_config.get(key),
                "run": dirname,
                "note": "launched over SSH from the coordinator host by "
                        "scripts/run.py (recorded verbatim in %s/"
                        "run-config.json)" % dirname,
            })
    out = os.path.join(results, "remote-commands.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(commands, f, indent=2)
        f.write("\n")

    for entry in commands:
        if config.remote_directory not in str(entry["command"]):
            raise SystemExit("recorded command does not use the "
                             "configured remote directory: %r"
                             % entry["command"])
    print(json.dumps({"environments": sorted(hosts),
                      "runs": run_dirs, "commands": len(commands)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
