#!/usr/bin/env python3
"""Bounded launcher for the churn experiment on the two lab VMs.

Runs one owner (rdma-server) and one requester (rdma-client) process over
SSH, fetches ledgers/logs/diagnostics into results/<run>/, and writes
run-config.json, stop-record.json and execution.json.  All remote files
live under /tmp/rkey-lifecycle-churn-glm only."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import ledger  # noqa: E402

REMOTE = "/tmp/rkey-lifecycle-churn-glm"
OWNER = "192.168.122.112"     # rdma-server, runs the owner
CLIENT = "192.168.122.111"    # rdma-client, runs the requester
SSH = ["ssh", "-F", "/dev/null", "-i", "/home/tianchi/epochrdma/keys/epochrdma_vm",
       "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
       "-o", "ConnectTimeout=8"]
SCP = ["scp", "-F", "/dev/null", "-i", "/home/tianchi/epochrdma/keys/epochrdma_vm",
       "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
PORT = 7471
GLOBAL_MAX_REGS = 1048576
GLOBAL_MAX_SECONDS = 900

ENV_CMD = ("uname -a; cat /proc/version; ibv_devices; "
           "cat /sys/module/rdma_rxe/version 2>/dev/null; "
           "ibv_devinfo -d rxe0 2>/dev/null | head -20")
DIAG_CMD = "dmesg | tail -120"


def utc():
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def ssh_run(ip, command, timeout=120, capture=True):
    return subprocess.run(SSH + ["ubuntu@" + ip, command], capture_output=capture,
                          timeout=timeout)


def fetch(ip, remote_path, local_path):
    r = subprocess.run(SCP + ["ubuntu@%s:%s" % (ip, remote_path), local_path],
                       capture_output=True, timeout=180)
    if r.returncode:
        print("scp failed %s:%s: %s" % (ip, remote_path,
                                        r.stderr.decode(errors="replace")))
    return r.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=["pilot", "main", "shakedown"])
    parser.add_argument("--max-regs", type=int, required=True)
    parser.add_argument("--deadline", type=int, required=True,
                        help="seconds from first owner registration")
    parser.add_argument("--label", default=None)
    parser.add_argument("--selftest", action="store_true",
                        help="enable the gen-1 machinery selftest (pilot only)")
    args = parser.parse_args()

    if args.max_regs > GLOBAL_MAX_REGS or args.deadline > GLOBAL_MAX_SECONDS:
        raise SystemExit("caps exceed the hard global budget")

    label = args.label or args.role
    out_dir = os.path.join(REPO, "results", "%s-%s" % (args.role, utc()))
    os.makedirs(out_dir, exist_ok=True)
    port = PORT + (0 if args.role != "shakedown" else 1)

    execution = {"role": args.role, "label": label, "max_regs": args.max_regs,
                 "deadline_seconds": args.deadline, "port": port,
                 "selftest": bool(args.selftest), "remote_directory": REMOTE,
                 "commands": [], "started_utc": utc()}

    # environments before
    for host, name in ((OWNER, "owner"), (CLIENT, "requester")):
        r = ssh_run(host, ENV_CMD)
        with open(os.path.join(out_dir, "%s-env-before.json" % name), "w") as f:
            json.dump({"host": host, "command": ENV_CMD,
                       "returncode": r.returncode,
                       "output": r.stdout.decode(errors="replace")}, f, indent=2)
        d = ssh_run(host, DIAG_CMD)
        with open(os.path.join(out_dir, "%s-dmesg-before.txt" % name), "w") as f:
            f.write(d.stdout.decode(errors="replace"))

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
    rq = subprocess.run(SSH + ["ubuntu@" + CLIENT, req_cmd], capture_output=True,
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

    # environments + diagnostics after
    for host, name in ((OWNER, "owner"), (CLIENT, "requester")):
        r = ssh_run(host, ENV_CMD)
        with open(os.path.join(out_dir, "%s-env-after.json" % name), "w") as f:
            json.dump({"host": host, "command": ENV_CMD,
                       "returncode": r.returncode,
                       "output": r.stdout.decode(errors="replace")}, f, indent=2)
        d = ssh_run(host, DIAG_CMD)
        with open(os.path.join(out_dir, "%s-dmesg-after.txt" % name), "w") as f:
            f.write(d.stdout.decode(errors="replace"))
        diag = {"available": d.returncode == 0, "command": DIAG_CMD,
                "returncode": d.returncode,
                "new_abnormal_lines": []}
        before = os.path.join(out_dir, "%s-dmesg-before.txt" % name)
        after = os.path.join(out_dir, "%s-dmesg-after.txt" % name)
        if d.returncode == 0 and os.path.exists(before):
            old = set(open(before).read().splitlines())
            new = [ln for ln in open(after).read().splitlines() if ln not in old]
            bad = [ln for ln in new if ("WARNING" in ln or "BUG" in ln
                                        or "cleanup" in ln.lower())]
            diag["new_lines_count"] = len(new)
            diag["new_abnormal_lines"] = bad
        with open(os.path.join(out_dir, "%s-diagnostics.json" % name), "w") as f:
            json.dump(diag, f, indent=2)

    # run config + stop record from the owner ledger
    run_config = {"max_wall_seconds": args.deadline,
                  "max_target_registrations": args.max_regs,
                  "calibration_prefix_registrations": min(4096, args.max_regs),
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
