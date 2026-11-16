"""Shared configuration for the two-endpoint RXE lab launchers."""

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / "config.local.toml"
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_REMOTE = re.compile(r"/[A-Za-z0-9_./-]+\Z")


class ConfigError(ValueError):
    """A lab configuration is missing or invalid."""


@dataclass(frozen=True)
class LabConfig:
    owner_ip: str
    requester_ip: str
    ssh_user: str
    ssh_identity_file: str
    ssh_config_file: str | None
    remote_directory: str

    def target(self, ip: str) -> str:
        return f"{self.ssh_user}@{ip}"

    def ssh_args(self, command: str = "ssh") -> list[str]:
        args = [command]
        if self.ssh_config_file:
            args += ["-F", self.ssh_config_file]
        args += ["-i", self.ssh_identity_file,
                 "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        if command == "ssh":
            args += ["-o", "ConnectTimeout=8"]
        return args


def load_config(path: str | os.PathLike[str] | None = None) -> LabConfig:
    """Load and validate a TOML config. The key path is read, never key data."""
    filename = Path(path or os.environ.get("RXE_CHURN_CONFIG", DEFAULT_PATH)).expanduser()
    try:
        with filename.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load {filename}: {exc}") from exc
    lab = data.get("lab")
    if not isinstance(lab, dict):
        raise ConfigError("config needs a [lab] table")
    required = ("owner_ip", "requester_ip", "ssh_user",
                "ssh_identity_file", "remote_directory")
    for name in required:
        if not isinstance(lab.get(name), str) or not lab[name].strip():
            raise ConfigError(f"lab.{name} must be a nonempty string")
    try:
        owner = str(ipaddress.IPv4Address(lab["owner_ip"]))
        requester = str(ipaddress.IPv4Address(lab["requester_ip"]))
    except ipaddress.AddressValueError as exc:
        raise ConfigError("owner_ip and requester_ip must be IPv4 addresses") from exc
    if owner == requester:
        raise ConfigError("owner_ip and requester_ip must differ")
    user = lab["ssh_user"]
    if not _USER.fullmatch(user):
        raise ConfigError("lab.ssh_user contains unsupported characters")
    remote = lab["remote_directory"]
    if not _REMOTE.fullmatch(remote) or ".." in Path(remote).parts:
        raise ConfigError("lab.remote_directory must be a safe absolute path")
    key_path = Path(lab["ssh_identity_file"]).expanduser()
    if not key_path.is_absolute():
        key_path = filename.parent / key_path
    key = str(key_path.resolve())
    ssh_config = lab.get("ssh_config_file")
    if ssh_config is not None:
        if not isinstance(ssh_config, str) or not ssh_config.strip():
            raise ConfigError("lab.ssh_config_file must be a nonempty path if set")
        config_path = Path(ssh_config).expanduser()
        if not config_path.is_absolute():
            config_path = filename.parent / config_path
        ssh_config = str(config_path.resolve())
    return LabConfig(owner, requester, user, key, ssh_config, remote)
