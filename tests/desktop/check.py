#!/usr/bin/env python3
"""Check the desktop service with synthetic data; temporary outputs clean up automatically."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts"


def run(out):
    out = out.resolve()
    if not out.is_relative_to(ARTIFACTS):
        raise ValueError("output must be under repository artifacts")
    out.mkdir(parents=True, exist_ok=True)
    (out / "tmp").mkdir(exist_ok=True)
    responses = Path(tempfile.mkdtemp(prefix="responses-", dir=out))
    env = {k: v for k, v in os.environ.items() if not k.startswith("METER_")}
    env.update(TMPDIR=str(out / "tmp"), PYTHONDONTWRITEBYTECODE="1",
               COLLECTOR_TEST_SNAPSHOTS_DIR=str(out / "synthetic"),
               API_TEST_RESPONSES_DIR=str(responses))

    def check(command):
        subprocess.run(command, cwd=ROOT, env=env, check=True)

    sources = sorted(str(p) for folder in (ROOT / "cmd", ROOT / "internal") for p in folder.rglob("*.go"))
    if subprocess.check_output(["gofmt", "-l", *sources], text=True):
        raise RuntimeError("Go sources require gofmt")
    check(["go", "test", "-race", "-count=1", "./..."])
    check(["go", "vet", "./..."])
    for name in ("meter-collector", "meter-api"):
        check(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(out / name), "./cmd/" + name])
    for version in (1, 2):
        check([sys.executable, "tests/contracts/validate.py", "--version", str(version)])
    for suite in ("contracts", "desktop"):
        check([sys.executable, "-m", "unittest", "discover", "-s", "tests/" + suite])

    spec = importlib.util.spec_from_file_location("oracle", ROOT / "tests/contracts/validate.py")
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    generated = list((out / "synthetic").glob("*.json"))
    if len(generated) != 52:
        raise RuntimeError("synthetic snapshot inventory changed")
    for path in generated:
        oracle.validate_snapshot(path.read_bytes(), version=2)
    oracle.validate_history((out / "synthetic/history/retained.json").read_bytes(), persisted=True)
    saved_responses = sorted(responses.glob("*.json"))
    if len(saved_responses) != 17:
        raise RuntimeError("API response inventory changed")
    for response in saved_responses:
        value = oracle.validate(response.read_bytes())
        if response.name == "missing.json":
            if value["observed_at"] is not None or value["updated_at"] is not None:
                raise RuntimeError("missing snapshot timestamp")
            continue
        name = "normal.json" if response.name.startswith("projection-") else response.name
        source = oracle.validate_snapshot((ROOT / "contracts/v1/fixtures" / name).read_bytes())
        if value != oracle.project(source, value["as_of"]):
            raise RuntimeError("projection diverged from independent oracle")

    collector = str(out / "meter-collector")
    result = subprocess.run([
        collector, "--once", "--timezone", "UTC", "--auth-file", str(out / "absent-auth/auth.json"),
        "--data-dir", str(out / "offline/data"), "--state-dir", str(out / "offline/state"),
    ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
    if result.returncode != 1 or result.stdout != '{"event":"auth_missing"}\n' or result.stderr != "source_unavailable\n":
        raise RuntimeError("offline CLI failure or sanitized logging check failed")
    oracle.validate_snapshot((out / "offline/data/usage.json").read_bytes(), version=2)
    for command in ([collector, "--collection-seconds", "0"],
                    [str(out / "meter-api"), "--token", "private-invalid-test-token"]):
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        if result.returncode != 2 or result.stdout or result.stderr != "invalid_configuration\n":
            raise RuntimeError("invalid CLI configuration or privacy check failed")
    check([sys.executable, "tests/desktop/history.py", "--output", str(out)])
    check(["git", "diff", "--check"])
    check(["git", "diff", "--cached", "--check"])
    print(json.dumps({"desktop_checks": "passed", "generated_snapshots": len(generated),
                      "oracle_responses": len(saved_responses)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Keep results in this directory under artifacts/")
    args = parser.parse_args()
    if args.output:
        run(args.output)
    else:
        ARTIFACTS.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="desktop-", dir=ARTIFACTS) as directory:
            run(Path(directory))
