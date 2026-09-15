#!/usr/bin/env python3
"""API pre-submission gates, including the existing collector regression wrapper."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=ROOT / "artifacts/cm-005/check")
    args = p.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to(ROOT / "artifacts"):
        p.error("output must be under repository artifacts")
    out.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("METER_")}
    response_dir = Path(tempfile.mkdtemp(prefix="responses-", dir=out))
    env["API_TEST_RESPONSES_DIR"] = str(response_dir)
    subprocess.run([sys.executable, "tools/collector-check/check.py", "--output", str(out / "collector")],
                   cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tools/api-check"],
                   cwd=ROOT, env=env, check=True)
    spec = importlib.util.spec_from_file_location("oracle", ROOT / "tools/contract-check/validate.py")
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    responses = sorted(response_dir.glob("*.json"))
    if len(responses) != 17:
        raise RuntimeError(f"unexpected response inventory: {len(responses)}")
    for response in responses:
        value = oracle.validate(response.read_bytes())
        if response.name.startswith("projection-"):
            source = oracle.validate_snapshot((ROOT / "contracts/v1/fixtures/normal.json").read_bytes())
        elif response.name == "missing.json":
            if value["observed_at"] is not None or value["updated_at"] is not None:
                raise RuntimeError("missing snapshot timestamp")
            continue
        else:
            source = oracle.validate_snapshot((ROOT / "contracts/v1/fixtures" / response.name).read_bytes())
        if value != oracle.project(source, value["as_of"]):
            raise RuntimeError("projection diverged from independent oracle")
    env.update(GOCACHE=str(out / "collector/go-cache"), TMPDIR=str(out / "collector/tmp"))
    binary = out / "meter-api"
    subprocess.run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(binary), "./cmd/meter-api"],
                   cwd=ROOT, env=env, check=True)
    result = subprocess.run([str(binary), "--token", "private-invalid-test-token"], env=env,
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 2 or result.stdout or result.stderr != "invalid_configuration\n":
        raise RuntimeError("CLI privacy/failure check")
    subprocess.run([sys.executable,"tools/api-check/history.py","--output",str(out)],cwd=ROOT,env=env,check=True)
    print(json.dumps({"local_gates": "passed", "oracle_responses": len(responses),
                      "largest_response_bytes": max(p.stat().st_size for p in responses)}))


if __name__ == "__main__":
    main()
