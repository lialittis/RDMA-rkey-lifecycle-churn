#!/usr/bin/env python3
"""Bounded launcher for the churn experiment on the two lab VMs.

Runs one owner (rdma-server) and one requester (rdma-client) process over
SSH, fetches ledgers/logs/diagnostics into results/<run>/, and writes
run-config.json, stop-record.json and execution.json. Remote paths and
endpoints come from the lab config."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import ledger  # noqa: E402
from lab_config import ConfigError, load_config  # noqa: E402

REMOTE = OWNER = CLIENT = SSH_USER = None
SSH = SCP = ENV_CMD = None
PORT = 7471
GLOBAL_MAX_SECONDS = 900

# Authorized per-role registration caps of the reproduction-validation run
# plan.  The internal diagnostic role is "smoke" (fits the preserved
# version-1 ledger's 8-byte run_role field); its USER-FACING label is
# "shakedown".  "pilot" belongs to the frozen original-run revision.
ROLE_MAX_REGS = {"smoke": 4096, "main": 600000}
RUN_ROLES = ("smoke", "main")


def validate_launch(role, max_regs, deadline):
    """Reject any launch outside the authorized per-role caps (offline,
    no network): role must be smoke/main, max_regs within the role cap,
    deadline within the 900 s aggregate wall budget."""
    if role not in ROLE_MAX_REGS:
        raise ValueError("role must be one of %r, got %r"
                         % (sorted(ROLE_MAX_REGS), role))
    if not 1 <= max_regs <= ROLE_MAX_REGS[role]:
        raise ValueError("max-regs for role %r must be within 1..%d, "
                         "got %d" % (role, ROLE_MAX_REGS[role], max_regs))
    if not 1 <= deadline <= GLOBAL_MAX_SECONDS:
        raise ValueError("deadline must be within 1..%d seconds, got %d"
                         % (GLOBAL_MAX_SECONDS, deadline))

DIAG_CMD = "dmesg; echo __DMESG_RC__=$?"
DMESG_RC_MARKER = "__DMESG_RC__="


def split_diag_capture(stdout, stderr):
    """Split a diagnostics capture into dmesg's stdout, its REAL exit
    status and its stderr.

    The historical form ``dmesg | tail -120`` lost dmesg's exit status to
    the pipe and discarded its stderr, so an unprivileged read failure
    looked like an empty-but-successful capture; a capture without the
    __DMESG_RC__ marker therefore yields returncode None and is never
    treated as valid."""
    def text(value):
        return value.decode(errors="replace") if isinstance(value, bytes) \
            else (value or "")
    rc = None
    lines = []
    for line in text(stdout).splitlines():
        if line.startswith(DMESG_RC_MARKER):
            try:
                rc = int(line[len(DMESG_RC_MARKER):].strip())
            except ValueError:
                rc = None
        else:
            lines.append(line)
    return {"dmesg_stdout": "\n".join(lines),
            "dmesg_returncode": rc,
            "dmesg_stderr": text(stderr)}


def capture_is_valid(cap):
    return (cap["dmesg_returncode"] == 0
            and not cap["dmesg_stderr"].strip())


def diagnostics_delta(before_cap, after_cap):
    """A kernel before/after delta may only be claimed when BOTH captures
    are valid (dmesg exited 0 with empty stderr).  Otherwise the delta
    status is 'unverified' and no clean/abnormal claim is made.

    A sudo-based capture taken after the fact is a POST-HOC snapshot,
    never a historical delta; it belongs in a separately labeled artifact
    (see scripts/posthoc_diagnostics.py)."""
    diag = {"delta_status": "unverified",
            "new_lines_count": None, "new_abnormal_lines": []}
    if not (capture_is_valid(before_cap) and capture_is_valid(after_cap)):
        return diag
    old = set(before_cap["dmesg_stdout"].splitlines())
    new = [ln for ln in after_cap["dmesg_stdout"].splitlines()
           if ln not in old]
    bad = [ln for ln in new if ("WARNING" in ln or "BUG" in ln
                                or "cleanup" in ln.lower())]
    diag["new_lines_count"] = len(new)
    diag["new_abnormal_lines"] = bad
    diag["delta_status"] = "abnormal-lines" if bad else "clean"
    return diag


def utc():
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def ssh_run(ip, command, timeout=120, capture=True):
    return subprocess.run(SSH + [SSH_USER + "@" + ip, command], capture_output=capture,
                          timeout=timeout)


def fetch(ip, remote_path, local_path):
    r = subprocess.run(SCP + ["%s@%s:%s" % (SSH_USER, ip, remote_path), local_path],
                       capture_output=True, timeout=180)
    if r.returncode:
        print("scp failed %s:%s: %s" % (ip, remote_path,
                                        r.stderr.decode(errors="replace")))
    return r.returncode == 0


def main():
    global REMOTE, OWNER, CLIENT, SSH_USER, SSH, SCP, ENV_CMD
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="TOML lab config (default: config.local.toml)")
    parser.add_argument("--role", required=True,
                        choices=list(RUN_ROLES),
                        help="smoke = short diagnostic auxiliary "
                             "(user-facing name: shakedown); main = "
                             "primary run")
    parser.add_argument("--max-regs", type=int, required=True)
    parser.add_argument("--deadline", type=int, required=True,
                        help="seconds from first owner registration")
    parser.add_argument("--label", default=None)
    parser.add_argument("--selftest", action="store_true",
                        help="enable the gen-1 machinery selftest "
                             "(smoke role only)")
    args = parser.parse_args()
    try:
        validate_launch(args.role, args.max_regs, args.deadline)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    REMOTE = config.remote_directory
    OWNER = config.owner_ip
    CLIENT = config.requester_ip
    SSH_USER = config.ssh_user
    SSH = config.ssh_args()
    SCP = config.ssh_args("scp")
    ENV_CMD = ("uname -a; cat /proc/version; ibv_devices; "
               "cat /sys/module/rdma_rxe/version 2>/dev/null; "
               "ibv_devinfo -d rxe0 2>/dev/null | head -20")

    # user-facing label: the diagnostic run is called the shakedown
    label = args.label or ("shakedown" if args.role == "smoke"
                           else args.role)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
        parser.error("label must contain only letters, digits, dot, dash or underscore")
    out_dir = os.path.join(REPO, "results", "%s-%s" % (args.role, utc()))
    os.makedirs(out_dir, exist_ok=True)
    port = PORT + (1 if args.role == "smoke" else 0)

    execution = {"role": args.role, "label": label, "max_regs": args.max_regs,
                 "deadline_seconds": args.deadline, "port": port,
                 "selftest": bool(args.selftest), "remote_directory": REMOTE,
                 "commands": [], "started_utc": utc()}

    # environments before
    diag_before = {}
    for host, name in ((OWNER, "owner"), (CLIENT, "requester")):
        r = ssh_run(host, ENV_CMD)
        with open(os.path.join(out_dir, "%s-env-before.json" % name), "w") as f:
            json.dump({"host": host, "command": ENV_CMD,
                       "returncode": r.returncode,
                       "output": r.stdout.decode(errors="replace")}, f, indent=2)
        d = ssh_run(host, DIAG_CMD)
        cap = split_diag_capture(d.stdout, d.stderr)
        diag_before[name] = cap
        with open(os.path.join(out_dir, "%s-dmesg-before.txt" % name), "w") as f:
            f.write(cap["dmesg_stdout"])

    owner_ledger = "%s/%s-owner.ledger" % (REMOTE, label)
    req_ledger = "%s/%s-requester.ledger" % (REMOTE, label)
    owner_cmd = ("%s/churn owner %s %s %d %s %s %d %d %s %s"
                 % (REMOTE, OWNER, CLIENT, port, args.role, label,
                    args.max_regs, args.deadline, owner_ledger,
                    "selftest" if args.selftest else "none"))
    req_cmd = ("%s/churn requester %s %s %d %s %s %d %d %s"
               % (REMOTE, CLIENT, OWNER, port, args.role, label,
                  args.max_regs, args.deadline, req_ledger))
    execution["commands"].append({"host": OWNER, "command": owner_cmd})
    execution["commands"].append({"host": CLIENT, "command": req_cmd})

    # owner in background (writes rc file), requester in foreground
    ssh_run(OWNER, "rm -f %s/%s-owner.rc %s/%s-owner.log %s/%s-owner.err"
            % (REMOTE, label, REMOTE, label, REMOTE, label))
    bg = ("nohup sh -c '%s > %s/%s-owner.log 2> %s/%s-owner.err; "
          "echo $? > %s/%s-owner.rc' >/dev/null 2>&1 &"
          % (owner_cmd, REMOTE, label, REMOTE, label, REMOTE, label))
    r = ssh_run(OWNER, bg)
    if r.returncode:
        raise SystemExit("failed to launch owner: %s"
                         % r.stderr.decode(errors="replace"))
    time.sleep(2)
    started = time.time()
    rq = subprocess.run(SSH + [SSH_USER + "@" + CLIENT, req_cmd], capture_output=True,
                        timeout=args.deadline + 240)
    execution["requester_returncode"] = rq.returncode
    # wait for the owner rc file (bounded)
    owner_rc = None
    while time.time() - started < args.deadline + 200:
        rc = ssh_run(OWNER, "cat %s/%s-owner.rc 2>/dev/null || true" % (REMOTE, label))
        text = rc.stdout.decode(errors="replace").strip()
        if text:
            owner_rc = int(text)
            break
        time.sleep(3)
    execution["owner_returncode"] = owner_rc
    execution["finished_utc"] = utc()

    # fetch artifacts
    fetch(OWNER, "%s/%s-owner.ledger" % (REMOTE, label),
          os.path.join(out_dir, "owner.ledger"))
    fetch(OWNER, "%s/%s-owner.log" % (REMOTE, label),
          os.path.join(out_dir, "owner.log"))
    fetch(OWNER, "%s/%s-owner.err" % (REMOTE, label),
          os.path.join(out_dir, "owner.err"))
    fetch(CLIENT, "%s/%s-requester.ledger" % (REMOTE, label),
          os.path.join(out_dir, "requester.ledger"))
    rqlog = ssh_run(CLIENT, "cat /tmp/%s-requester.log 2>/dev/null || true" % label)
    # requester ran in the foreground; capture its output directly
    with open(os.path.join(out_dir, "requester.log"), "wb") as f:
        f.write(rq.stdout)
    with open(os.path.join(out_dir, "requester.err"), "wb") as f:
        f.write(rq.stderr)

    # environments + diagnostics after: a delta claim requires BOTH
    # captures valid (dmesg exit 0, empty stderr); otherwise the kernel
    # delta is recorded as unverified, never as clean
    for host, name in ((OWNER, "owner"), (CLIENT, "requester")):
        r = ssh_run(host, ENV_CMD)
        with open(os.path.join(out_dir, "%s-env-after.json" % name), "w") as f:
            json.dump({"host": host, "command": ENV_CMD,
                       "returncode": r.returncode,
                       "output": r.stdout.decode(errors="replace")}, f, indent=2)
        d = ssh_run(host, DIAG_CMD)
        cap_after = split_diag_capture(d.stdout, d.stderr)
        with open(os.path.join(out_dir, "%s-dmesg-after.txt" % name), "w") as f:
            f.write(cap_after["dmesg_stdout"])
        cap_before = diag_before[name]
        diag = {"available": capture_is_valid(cap_before)
                and capture_is_valid(cap_after),
                "command": DIAG_CMD,
                "dmesg_returncode": cap_after["dmesg_returncode"],
                "dmesg_returncode_before": cap_before["dmesg_returncode"],
                "dmesg_stderr_excerpt": cap_after["dmesg_stderr"][:300],
                "dmesg_stderr_excerpt_before":
                    cap_before["dmesg_stderr"][:300]}
        diag.update(diagnostics_delta(cap_before, cap_after))
        with open(os.path.join(out_dir, "%s-diagnostics.json" % name), "w") as f:
            json.dump(diag, f, indent=2)

    # run config + stop record from the owner ledger
    run_config = {"owner_endpoint": OWNER,
                  "requester_endpoint": CLIENT,
                  "remote_directory": REMOTE,
                  "max_wall_seconds": args.deadline,
                  "max_target_registrations": args.max_regs,
                  "port": port, "selftest": bool(args.selftest),
                  "owner_command": owner_cmd, "requester_command": req_cmd}
    with open(os.path.join(out_dir, "run-config.json"), "w") as f:
        json.dump(run_config, f, indent=2)

    stop_reason = "explicit-stop"
    regs_at_stop = 0
    wall = 0.0
    owner_ledger_local = os.path.join(out_dir, "owner.ledger")
    if os.path.exists(owner_ledger_local) and os.path.getsize(owner_ledger_local) > 16:
        try:
            parsed = ledger.read_ledger(owner_ledger_local)
            regs = [r for r in parsed.records
                    if r["kind"] == "registration" and r["target"]]
            regs_at_stop = len(regs)
            monos = [r["mono_ns"] for r in parsed.records
                     if isinstance(r.get("mono_ns"), int)]
            if regs and monos:
                wall = (max(monos) - regs[0]["mono_ns"]) / 1e9
            for rec in parsed.records:
                if rec["kind"] == "run-meta" and rec.get("stop_reason"):
                    stop_reason = rec["stop_reason"]
        except ledger.LedgerIntegrityError as exc:
            stop_reason = "control-failure"
            print("owner ledger integrity failure: %s" % exc)
    else:
        stop_reason = "control-failure"
    if owner_rc not in (0, None):
        fallback = {3: "control-failure", 4: "memory-mismatch",
                    5: "cleanup-warning", 6: "probe-timeout",
                    7: "explicit-stop"}.get(owner_rc)
        if fallback and stop_reason in ("explicit-stop",) and fallback != "explicit-stop":
            stop_reason = fallback
    stop_record = {"stop_reason": stop_reason,
                   "registrations_at_stop": regs_at_stop,
                   "wall_seconds_at_stop": round(wall, 3),
                   "owner_returncode": owner_rc,
                   "requester_returncode": rq.returncode}
    with open(os.path.join(out_dir, "stop-record.json"), "w") as f:
        json.dump(stop_record, f, indent=2)
    with open(os.path.join(out_dir, "execution.json"), "w") as f:
        json.dump(execution, f, indent=2)

    print(json.dumps({"result_dir": out_dir, "stop_record": stop_record}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
