#!/usr/bin/env python3
"""Measure only the probe process tree; all output must stay under this repo."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def cpu_seconds(value):
    days, sep, rest = value.partition("-")
    day_seconds = int(days) * 86400 if sep else 0
    fields = (rest if sep else value).split(":")
    total = 0.0
    for field in fields:
        total = total * 60 + float(field)
    return total + day_seconds


def tree_sample(pid):
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,rss=,time="], text=True
    )
    rows = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 4:
            child, parent, rss, cpu = fields
            rows[int(child)] = (int(parent), int(rss), cpu_seconds(cpu))
    selected = {pid}
    while True:
        extended = selected | {child for child, row in rows.items() if row[0] in selected}
        if extended == selected:
            break
        selected = extended
    return {child: rows[child] for child in selected if child in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--warmup", type=int, default=30)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    binary, out = args.binary.resolve(), args.output.resolve()
    if not binary.is_relative_to(root) or not out.is_relative_to(root):
        parser.error("binary/output must be inside the current repository")
    if args.seconds < 1 or args.warmup < 0:
        parser.error("invalid measurement duration")
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    samples = []
    steady_started = None
    with (out / "polls.jsonl").open("w") as log:
        proc = subprocess.Popen(
            [str(binary), "-duration", f"{args.seconds + args.warmup + 30}s"],
            stdout=log, stderr=subprocess.DEVNULL,
        )
        try:
            next_sample = started
            while True:
                current = time.monotonic()
                if proc.poll() is not None:
                    raise RuntimeError("probe exited before measurement completed")
                elapsed = current - started
                tree = tree_sample(proc.pid)
                if proc.pid not in tree:
                    raise RuntimeError("probe missing from process sample")
                samples.append({"elapsed_seconds":elapsed, "rss_kib":sum(r[1] for r in tree.values()),
                                "processes":{str(p):{"rss_kib":r[1],"cpu_seconds":r[2]} for p,r in tree.items()}})
                if elapsed >= args.warmup and steady_started is None:
                    steady_started = elapsed
                if steady_started is not None and elapsed - steady_started >= args.seconds:
                    break
                next_sample += 1 if elapsed < args.warmup else 5
                time.sleep(max(0, next_sample-time.monotonic()))
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
    steady = [s for s in samples if s["elapsed_seconds"] >= args.warmup]
    first, last = steady[0], steady[-1]
    duration = last["elapsed_seconds"] - first["elapsed_seconds"]
    baseline = {p:r["cpu_seconds"] for p,r in first["processes"].items()}
    final_cpu = {}
    for sample in steady:
        final_cpu.update({p:r["cpu_seconds"] for p,r in sample["processes"].items()})
    cpu = sum(v-baseline.get(p,0) for p,v in final_cpu.items())
    polls = [json.loads(line) for line in (out/"polls.jsonl").read_text().splitlines()]
    summary = {
        "steady_elapsed_seconds":duration, "steady_samples":len(steady),
        "steady_max_rss_mib":max(s["rss_kib"] for s in steady)/1024,
        "startup_sampled_peak_rss_mib":max(s["rss_kib"] for s in samples if s["elapsed_seconds"]<=args.warmup)/1024,
        "steady_cpu_seconds":cpu, "average_cpu_percent_one_core":100*cpu/duration,
        "observed_runtime_processes":len({p for s in samples for p in s["processes"]}),
        "total_polls":len(polls), "successful_polls":sum(p.get("bucket")=="codex" for p in polls),
        "failed_polls":sum(p.get("status")=="unavailable" for p in polls),
        "instrumentation_excluded":"Python harness and transient ps sampling; neither ships with runtime",
        "rss_definition":"ps RSS, not macOS physical footprint; sample max, not continuous peak",
    }
    (out/"samples.json").write_text(json.dumps(samples,indent=2)+"\n")
    (out/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2),flush=True)


if __name__ == "__main__":
    main()
