#!/usr/bin/env python3
"""Bounded macOS collector measurement; private raw results stay in this repo."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--probe-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--warmup", type=int, default=10)
    args = parser.parse_args()
    binary, probe, out = args.binary.resolve(), args.probe_binary.resolve(), args.output.resolve()
    if not all(p.is_relative_to(ROOT) for p in (binary, probe)) or not out.is_relative_to(ROOT / "artifacts"):
        parser.error("binaries must stay inside this repository; output must be under ignored artifacts")
    if args.seconds < 120 or args.warmup < 0:
        parser.error("need at least 120 seconds to observe three default polls")
    os.umask(0o077)
    out.mkdir(parents=True, exist_ok=False)
    oracle = module("contract_oracle", ROOT / "tools/contract-check/validate.py")
    metrics = module("probe_metrics", ROOT / "tools/collector-probe/measure.py")
    samples, snapshots, comparisons = [], [], []
    started = time.monotonic()
    steady_started = None
    command = [str(binary), "--data-dir", str(out / "data"), "--state-dir", str(out / "state")]
    env = {key: value for key, value in os.environ.items() if not key.startswith("METER_")}
    with (out / "events.jsonl").open("w") as log:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            next_sample = started
            while True:
                current = time.monotonic()
                elapsed = current - started
                if proc.poll() is not None:
                    raise RuntimeError("collector exited before measurement completed")
                tree = metrics.tree_sample(proc.pid)
                if proc.pid not in tree:
                    raise RuntimeError("collector absent from process sample")
                samples.append({
                    "elapsed_seconds": elapsed, "rss_kib": sum(row[1] for row in tree.values()),
                    "processes": {str(pid): {"rss_kib": row[1], "cpu_seconds": row[2]}
                                  for pid, row in tree.items()},
                })
                path = out / "data/usage.json"
                if path.exists():
                    raw = path.read_bytes()
                    snapshot = oracle.validate_snapshot(raw,version=2)
                    if not snapshots or snapshot["updated_at"] != snapshots[-1]["updated_at"]:
                        if snapshot["status"] != "ok":
                            raise RuntimeError("live source did not produce a fresh observation")
                        snapshots.append(snapshot)
                        # Independent feasibility executable; its process and CPU are
                        # instrumentation, not descendants of the measured collector.
                        result = subprocess.run([str(probe)], env=env, capture_output=True, timeout=15, check=True)
                        comparison = json.loads(result.stdout)
                        if comparison.get("bucket") != "codex":
                            raise RuntimeError("comparison probe had no general quota")
                        comparisons.append({
                            "remaining_difference_pp": abs(snapshot["remaining_percent"] - comparison["remaining_percent"]),
                            "reset_difference_seconds": abs(snapshot["reset_at"] - comparison["resets_at"]),
                            "observation_difference_seconds": abs(snapshot["observed_at"] - comparison["observed_at"]),
                        })
                        (out / "snapshots.json").write_text(json.dumps(snapshots))
                if elapsed >= args.warmup and steady_started is None:
                    steady_started = elapsed
                if steady_started is not None and elapsed - steady_started >= args.seconds:
                    break
                next_sample += 1 if elapsed < args.warmup else 5
                time.sleep(max(0, next_sample - time.monotonic()))
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("collector did not exit cleanly on SIGTERM")
    steady = [sample for sample in samples if sample["elapsed_seconds"] >= args.warmup]
    first, last = steady[0], steady[-1]
    duration = last["elapsed_seconds"] - first["elapsed_seconds"]
    baseline = {pid: row["cpu_seconds"] for pid, row in first["processes"].items()}
    final_cpu = {}
    for sample in steady:
        final_cpu.update({pid: row["cpu_seconds"] for pid, row in sample["processes"].items()})
    cpu = sum(value - baseline.get(pid, 0) for pid, value in final_cpu.items())
    summary = {
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "environment": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "steady_elapsed_seconds": duration, "steady_samples": len(steady),
        "steady_max_rss_mib": max(s["rss_kib"] for s in steady) / 1024,
        "startup_sampled_peak_rss_mib": max(s["rss_kib"] for s in samples if s["elapsed_seconds"] <= first["elapsed_seconds"]) / 1024,
        "steady_cpu_seconds": cpu, "average_cpu_percent_one_core": 100 * cpu / duration,
        "observed_runtime_processes": len({pid for s in samples for pid in s["processes"]}),
        "successful_polls": len(snapshots),
        "poll_spacing_seconds": [b["observed_at"] - a["observed_at"] for a, b in zip(snapshots, snapshots[1:])],
        "max_snapshot_bytes": max(len(oracle.encode(s)) for s in snapshots),
        "max_remaining_difference_pp": max(c["remaining_difference_pp"] for c in comparisons),
        "max_reset_difference_seconds": max(c["reset_difference_seconds"] for c in comparisons),
        "max_observation_difference_seconds": max(c["observation_difference_seconds"] for c in comparisons),
        "clean_sigterm": True,
        "instrumentation_excluded": "Python harness, transient ps and independent comparison probe",
        "scope": "preliminary collector-only sample; not the integrated 15-minute helper/API or device acceptance gate",
        "rss_definition": "sampled ps RSS, not continuous peak or macOS physical footprint",
    }
    (out / "samples.json").write_text(json.dumps(samples, indent=2) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    if (len(snapshots) < 3 or summary["steady_max_rss_mib"] >= 64 or
            summary["average_cpu_percent_one_core"] >= 1 or
            summary["max_remaining_difference_pp"] > 1 or
            summary["max_reset_difference_seconds"] > 120 or
            summary["max_observation_difference_seconds"] > 120):
        raise RuntimeError("preliminary validation gate failed")


if __name__ == "__main__":
    main()
