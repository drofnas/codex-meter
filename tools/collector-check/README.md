# Native collector validation

Use Python 3.11+ with `tools/contract-check/requirements.txt` installed and Go
1.26.8 or a newer patched release. The runtime itself has no Python or third-party Go dependency.

From the repository root, run with the contract-check interpreter:

```sh
.venv/bin/python tools/collector-check/check.py --output artifacts/cm-004/check
```

This is the collector's local pre-submission gate: formatting, race-enabled unit
and crash/restart tests, static checks, stripped native build, probe regressions,
all contract fixture classifications and semantic tests, generated snapshots
checked by the independent Python oracle, CLI failure/privacy checks, and diff
hygiene. Generated files, Go cache and test temporary directories stay under
ignored `artifacts/`. No real credentials, network, Docker or device is used.

CM-004 includes full simulated cycles, duplicate/conflicting/late observations,
gaps, boundary crossings, scheduled and ambiguous resets, DST/timezone changes,
private-state migration/corruption, and process crashes before private rename,
between private/public rename, and after public rename. The independent oracle
checks 51 generated envelopes, including 40 history scenarios. State and public
snapshot size assertions retain the 4,096-byte bounds.

Measure the history engine and atomic publication path without source access:

```sh
mkdir -p artifacts/cm-004/tmp
GOCACHE="$PWD/artifacts/cm-004/go-cache" TMPDIR="$PWD/artifacts/cm-004/tmp" \
  go test ./internal/collector -run '^$' -bench '^BenchmarkHistory' -benchtime=1s -count=5
```

Run benchmarks without other test/build workloads. The publication benchmark
includes two fsynced file replacements, with synthetic minute-spaced observations;
it measures wall time and Go allocations, not steady runtime RSS or CPU use.
Keep baseline and candidate measurements in separate ignored artifact directories
and use the same publication benchmark to compare them.

CM-003 also requires three live polls and a preliminary process-tree sample:

```sh
.venv/bin/python tools/collector-check/measure.py \
  --binary artifacts/cm-003/check/meter-collector \
  --probe-binary artifacts/cm-001/collector-probe \
  --output artifacts/cm-003/live-new --seconds 180 --warmup 10
```

The output directory must not already exist. Build the independent comparison
probe using its own [instructions](../collector-probe/README.md) if needed.
Measurement requires macOS `ps` permission, a signed-in file-backed Codex
account and network access to the fixed usage endpoint. It starts the actual
collector with default 60-second polling, validates every sampled publication,
compares each with the independent feasibility executable, samples runtime RSS
and CPU every five seconds after warmup, and terminates via SIGTERM. It records
only a sanitized aggregate on stdout. Private snapshots and raw measurements
remain in the ignored owner-only output directory.

The instrumenting Python process, `ps`, and comparison probe are excluded from
the collector's process tree. RSS is sampled resident memory, not continuous
peak or macOS physical footprint. The 64 MiB / 1% ceilings apply to the combined
future collector and API; this preliminary collector-only check leaves that
shared budget's remaining headroom visible. The full 15-minute combined
measurement and device soak remain CM-010 acceptance work.
