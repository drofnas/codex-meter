#!/usr/bin/env python3
"""Start the private Docker API using validated literal host settings."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "METER_AUTH_FILE": "~/.codex/auth.json", "METER_DATA_DIR": ".local/data",
    "METER_STATE_DIR": ".local/state", "METER_TIMEZONE": "",
    "METER_COLLECTION_SECONDS": "60", "METER_STALE_SECONDS": "180",
    "METER_SOURCE_TIMEOUT_SECONDS": "10", "METER_API_BIND_ADDRESS": "127.0.0.1",
    "METER_API_PORT": "8080", "METER_API_TOKEN": "",
}
OPTIONS = {key.removeprefix("METER_").lower().replace("_", "-"): key for key in DEFAULTS}


def integer(value, low, high):
    if not re.fullmatch(r"[0-9]+", value) or not low <= int(value) <= high:
        raise ValueError("invalid_configuration")
    return int(value)


def resolve(value, base):
    if not value or "\0" in value:
        raise ValueError("invalid_configuration")
    path = Path.home() / value[2:] if value.startswith("~/") else Path(value)
    if not path.is_absolute():
        path = base / path
    # Refuse a dangling symlink even when an ordinary missing leaf is allowed.
    for part in (path, *path.parents):
        if part.is_symlink() and not part.exists():
            raise ValueError("invalid_configuration")
    return path.resolve()


def settings(args, environ, cwd):
    values = DEFAULTS.copy()
    base = cwd
    if args.env_file is not None:
        path = resolve(args.env_file, cwd)
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("invalid_configuration")
        seen = set()
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not key or key.strip() != key or key in seen:
                raise ValueError("invalid_configuration")
            seen.add(key)
            if key.startswith("METER_") and key not in DEFAULTS:
                raise ValueError("invalid_configuration")
            if key in DEFAULTS:
                values[key] = value
        base = path.parent
    for key, value in environ.items():
        if key.startswith("METER_"):
            if key not in DEFAULTS:
                raise ValueError("invalid_configuration")
            values[key] = value
    for option, key in OPTIONS.items():
        value = getattr(args, option.replace("-", "_"))
        if value is not None:
            values[key] = value
    return values, base


def load(args, environ, cwd):
    values, base = settings(args, environ, cwd)
    data, state, auth = (resolve(values[key], base) for key in (
        "METER_DATA_DIR", "METER_STATE_DIR", "METER_AUTH_FILE"))
    paths = (data, state, auth.parent)
    if any(a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError("invalid_configuration")
    if not data.is_dir():
        raise ValueError("invalid_configuration")
    bind = ipaddress.ip_address(values["METER_API_BIND_ADDRESS"])
    if bind.is_unspecified or bind.is_multicast:
        raise ValueError("invalid_configuration")
    integer(values["METER_API_PORT"], 1024, 65535)
    poll = integer(values["METER_COLLECTION_SECONDS"], 10, 300)
    stale = integer(values["METER_STALE_SECONDS"], 30, 3600)
    timeout = integer(values["METER_SOURCE_TIMEOUT_SECONDS"], 1, 30)
    if stale < poll * 2 or timeout > poll:
        raise ValueError("invalid_configuration")
    if not re.fullmatch(r"[a-f0-9]{64}", values["METER_API_TOKEN"]):
        raise ValueError("invalid_configuration")
    name = values["METER_TIMEZONE"]
    if not name:
        _, sep, name = str(Path("/etc/localtime").resolve()).partition("/zoneinfo/")
        if not sep:
            raise ValueError("invalid_configuration")
    if not name or len(name) > 64 or name == "Local":
        raise ValueError("invalid_configuration")
    ZoneInfo(name)
    # Do not pass the host's METER_AUTH_FILE/STATE_DIR or unknown settings.
    result = {key: values[key] for key in (
        "METER_API_BIND_ADDRESS", "METER_API_PORT", "METER_API_TOKEN", "METER_STALE_SECONDS")}
    result.update(METER_DATA_DIR=str(data), METER_TIMEZONE=name,
                  API_DATA_GID=str(args.gid), API_MEMORY_MIB=str(args.memory_mib),
                  API_GO_MEMORY_MIB=str(args.memory_mib - 8),
                  API_PIDS_LIMIT=str(args.pids_limit), API_CPU_LIMIT=str(args.cpu_limit))
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("check", "up", "down", "status", "build"))
    p.add_argument("--env-file", default=None)
    for option in OPTIONS:
        p.add_argument("--" + option, default=None)
    p.add_argument("--gid", type=int, default=os.getgid())
    p.add_argument("--memory-mib", type=int, choices=range(16, 49), default=32)
    p.add_argument("--pids-limit", type=int, choices=range(32, 129), default=64)
    p.add_argument("--cpu-limit", type=float, default=0.25)
    return p


def main():
    args = parser().parse_args()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("METER_", "API_", "COMPOSE_"))}
    if args.action in ("down", "status"):
        # Existing resources are identified by project/service labels. Stopping
        # must still work after a token or data directory becomes unavailable.
        minimal = json.dumps({"services": {"api": {"image": "codex-meter-api:local"}}})
        return subprocess.run(["docker", "compose", "--project-name", "codex-meter",
                               "--env-file", os.devnull, "--file", "-",
                               "down" if args.action == "down" else "ps"],
                              input=minimal, text=True, cwd=ROOT, env=env).returncode
    try:
        if not 1 <= args.gid <= 2147483647 or not 0.01 <= args.cpu_limit <= 1:
            raise ValueError("invalid_configuration")
        resolved = load(args, os.environ, Path.cwd())
    except (OSError, ValueError, RuntimeError, ZoneInfoNotFoundError):
        print("invalid_configuration", file=sys.stderr)
        return 2
    # Disable Compose's automatic dotenv load and resolve against the repo.
    env.update(resolved)
    action = {"check": ["config", "--quiet"], "up": ["up", "--detach", "--build"],
              "down": ["down"], "status": ["ps"], "build": ["build"]}[args.action]
    return subprocess.call(["docker", "compose", "--project-name", "codex-meter",
                            "--env-file", os.devnull, "--file", str(ROOT / "compose.yaml"),
                            *action], cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
