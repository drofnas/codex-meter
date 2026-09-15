#!/usr/bin/env python3
"""Run local collector gates; generated outputs remain ignored in the repository."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/collector-check")
    args = parser.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to(ROOT / "artifacts"):
        parser.error("output must be under the repository's ignored artifacts directory")
    out.mkdir(parents=True, exist_ok=True)
    (out / "tmp").mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("METER_")}
    env.update(TMPDIR=str(out / "tmp"), GOCACHE=str(out / "go-cache"),
               COLLECTOR_TEST_SNAPSHOTS_DIR=str(out / "synthetic"))

    def run(command, cwd=ROOT):
        subprocess.run(command, cwd=cwd, env=env, check=True)

    sources = sorted(str(p) for folder in (ROOT / "cmd", ROOT / "internal") for p in folder.rglob("*.go"))
    unformatted = subprocess.check_output(["gofmt", "-l", *sources], text=True)
    if unformatted:
        raise RuntimeError("Go sources require gofmt")
    run(["go", "test", "-race", "-count=1", "./..."])
    run(["go", "vet", "./..."])
    binary = out / "meter-collector"
    run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(binary), "./cmd/meter-collector"])
    run(["go", "test", "-race", "-count=1", "./..."], ROOT / "tools/collector-probe")
    run(["go", "vet", "./..."], ROOT / "tools/collector-probe")
    run([sys.executable, "tools/contract-check/validate.py"])
    run([sys.executable, "tools/contract-check/validate.py", "--version", "2"])
    run([sys.executable, "-m", "unittest", "discover", "-s", "tools/contract-check"])
    spec = importlib.util.spec_from_file_location("oracle", ROOT / "tools/contract-check/validate.py")
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    generated = list((out / "synthetic").glob("*.json"))
    if len(generated) != 51:
        raise RuntimeError("synthetic snapshot inventory changed")
    for path in generated:
        oracle.validate_snapshot(path.read_bytes(),version=2)
    archive=out/"synthetic/history/retained.json"
    oracle.validate_history(archive.read_bytes(),persisted=True)
    result = subprocess.run([
        str(binary), "--once", "--timezone", "UTC", "--auth-file", str(out / "absent-auth/auth.json"),
        "--data-dir", str(out / "offline/data"), "--state-dir", str(out / "offline/state"),
    ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
    if result.returncode != 1 or result.stdout != '{"event":"auth_missing"}\n' or result.stderr != "source_unavailable\n":
        raise RuntimeError("offline CLI failure or sanitized logging check failed")
    oracle.validate_snapshot((out / "offline/data/usage.json").read_bytes(),version=2)
    result = subprocess.run([str(binary), "--collection-seconds", "0"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 2 or result.stderr != "invalid_configuration\n" or result.stdout:
        raise RuntimeError("invalid CLI configuration check failed")
    run(["git", "diff", "--check"])
    run(["git", "diff", "--cached", "--check"])
    print(json.dumps({"local_gates": "passed", "generated_snapshots": len(generated),
                      "largest_generated_bytes": max(p.stat().st_size for p in generated)}))


if __name__ == "__main__":
    main()
