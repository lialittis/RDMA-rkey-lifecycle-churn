#!/usr/bin/env python3
"""Build churn.c on rdma-client and deploy the binary to both lab VMs
under /tmp/rkey-lifecycle-churn-glm.  Records build provenance in
results/build.json (command, exit code, output, hashes)."""

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE = "/tmp/rkey-lifecycle-churn-glm"
OWNER = "192.168.122.112"
CLIENT = "192.168.122.111"
SSH = ["ssh", "-F", "/dev/null", "-i", "/home/tianchi/epochrdma/keys/epochrdma_vm",
       "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
       "-o", "ConnectTimeout=8"]
BUILD_CMD = ("gcc -std=c11 -Wall -Wextra -Werror -O2 -g %s/churn.c -o %s/churn "
             "$(pkg-config --cflags --libs libibverbs)" % (REMOTE, REMOTE))


def remote(ip, command, data=None, timeout=120):
    result = subprocess.run(SSH + ["ubuntu@" + ip, command], input=data,
                            capture_output=True, timeout=timeout)
    rec = {"host": ip, "command": command, "returncode": result.returncode,
           "stdout": result.stdout.decode(errors="replace")[:4000],
           "stderr": result.stderr.decode(errors="replace")[:4000]}
    if result.returncode:
        print(json.dumps(rec, indent=2))
        raise SystemExit(result.returncode)
    return result


def main():
    src_path = os.path.join(REPO, "src", "churn.c")
    src = open(src_path, "rb").read()
    src_sha = hashlib.sha256(src).hexdigest()

    remote(CLIENT, "mkdir -p %s && cat > %s/churn.c"
           % (shlex.quote(REMOTE), shlex.quote(REMOTE)), src)
    build = subprocess.run(
        SSH + ["ubuntu@" + CLIENT, BUILD_CMD], capture_output=True, timeout=300)
    build_rec = {"command": BUILD_CMD, "host": CLIENT,
                 "exit_code": build.returncode,
                 "stdout": build.stdout.decode(errors="replace")[-8000:],
                 "stderr": build.stderr.decode(errors="replace")[-8000:]}
    if build.returncode:
        print(build_rec["stderr"])
        raise SystemExit(build.returncode)

    fetch = remote(CLIENT, "base64 -w0 %s/churn" % shlex.quote(REMOTE))
    binary = base64.b64decode(fetch.stdout, validate=True)
    bin_sha = hashlib.sha256(binary).hexdigest()
    local_bin = os.path.join(REPO, "results", "churn.aarch64")
    os.makedirs(os.path.dirname(local_bin), exist_ok=True)
    with open(local_bin, "wb") as f:
        f.write(binary)
    build_rec["binary_sha256"] = bin_sha
    build_rec["source_sha256"] = src_sha
    build_rec["remote_directory"] = REMOTE
    build_rec["compiler_host"] = CLIENT
    build_rec["runtime_hosts"] = [CLIENT, OWNER]
    with open(os.path.join(REPO, "results", "build.json"), "w") as f:
        json.dump(build_rec, f, indent=2)
        f.write("\n")

    remote(OWNER, "mkdir -p %s && cat > %s/churn" % (shlex.quote(REMOTE),
                                                     shlex.quote(REMOTE)), binary)
    remote(OWNER, "chmod 700 %s/churn" % shlex.quote(REMOTE))
    remote(CLIENT, "chmod 700 %s/churn" % shlex.quote(REMOTE))
    for ip in (OWNER, CLIENT):
        remote(ip, "sha256sum %s/churn && ldd %s/churn | head -5"
               % (shlex.quote(REMOTE), shlex.quote(REMOTE)))
    print(json.dumps({"result": "compiled and deployed",
                      "source_sha256": src_sha, "binary_sha256": bin_sha}))


if __name__ == "__main__":
    sys.exit(main())
