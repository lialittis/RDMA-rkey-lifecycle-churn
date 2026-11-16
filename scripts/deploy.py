#!/usr/bin/env python3
"""Build churn.c on the requester and deploy it to both configured VMs."""

import argparse
import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from lab_config import ConfigError, load_config  # noqa: E402


def remote(config, ip, command, data=None, timeout=120):
    result = subprocess.run(config.ssh_args() + [config.target(ip), command], input=data,
                            capture_output=True, timeout=timeout)
    rec = {"host": ip, "command": command, "returncode": result.returncode,
           "stdout": result.stdout.decode(errors="replace")[:4000],
           "stderr": result.stderr.decode(errors="replace")[:4000]}
    if result.returncode:
        print(json.dumps(rec, indent=2))
        raise SystemExit(result.returncode)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="TOML lab config (default: config.local.toml)")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    remote_dir = config.remote_directory
    owner = config.owner_ip
    client = config.requester_ip
    build_cmd = ("gcc -std=c11 -Wall -Wextra -Werror -O2 -g %s/churn.c -o %s/churn "
                 "$(pkg-config --cflags --libs libibverbs)" % (remote_dir, remote_dir))
    src_path = os.path.join(REPO, "src", "churn.c")
    src = open(src_path, "rb").read()
    src_sha = hashlib.sha256(src).hexdigest()

    remote(config, client, "mkdir -p %s && cat > %s/churn.c"
           % (shlex.quote(remote_dir), shlex.quote(remote_dir)), src)
    build = subprocess.run(
        config.ssh_args() + [config.target(client), build_cmd], capture_output=True, timeout=300)
    build_rec = {"command": build_cmd, "host": client,
                 "exit_code": build.returncode,
                 "stdout": build.stdout.decode(errors="replace")[-8000:],
                 "stderr": build.stderr.decode(errors="replace")[-8000:]}
    if build.returncode:
        print(build_rec["stderr"])
        raise SystemExit(build.returncode)

    fetch = remote(config, client, "base64 -w0 %s/churn" % shlex.quote(remote_dir))
    binary = base64.b64decode(fetch.stdout, validate=True)
    bin_sha = hashlib.sha256(binary).hexdigest()
    local_bin = os.path.join(REPO, "results", "churn.aarch64")
    os.makedirs(os.path.dirname(local_bin), exist_ok=True)
    with open(local_bin, "wb") as f:
        f.write(binary)
    build_rec["binary_sha256"] = bin_sha
    build_rec["source_sha256"] = src_sha
    build_rec["remote_directory"] = remote_dir
    build_rec["compiler_host"] = client
    build_rec["runtime_hosts"] = [client, owner]
    with open(os.path.join(REPO, "results", "build.json"), "w") as f:
        json.dump(build_rec, f, indent=2)
        f.write("\n")

    remote(config, owner, "mkdir -p %s && cat > %s/churn" % (shlex.quote(remote_dir),
                                                              shlex.quote(remote_dir)), binary)
    remote(config, owner, "chmod 700 %s/churn" % shlex.quote(remote_dir))
    remote(config, client, "chmod 700 %s/churn" % shlex.quote(remote_dir))
    for ip in (owner, client):
        remote(config, ip, "sha256sum %s/churn && ldd %s/churn | head -5"
               % (shlex.quote(remote_dir), shlex.quote(remote_dir)))
    print(json.dumps({"result": "compiled and deployed",
                      "source_sha256": src_sha, "binary_sha256": bin_sha}))


if __name__ == "__main__":
    sys.exit(main())
