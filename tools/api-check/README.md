# API validation

Use the existing Python 3.11+ contract environment and Go toolchain. Run from
this repository; all outputs and temporary data stay in ignored `artifacts/`.
The HTTP integration tests need permission to open local sockets.

```sh
.venv/bin/python tools/api-check/check.py
```

This pre-submission gate runs the complete collector wrapper (Go formatting,
race tests, vet, native build, probe regressions, 51 contract fixtures, 17 semantic
tests and 51 generated collector envelopes). It also checks launcher configuration,
17 exported API responses against the independent oracle and exact reference
projection, an API build and sanitized CLI failure. API tests cover HTTP precedence,
all usage fixtures, missing/unreadable/oversized inputs, strict keys and numbers,
backwards clocks, reset/age boundaries, concurrency and atomic replacements.

With Docker running, build and test the actual runtime:

```sh
docker build --tag codex-meter-api:cm005 .
.venv/bin/python tools/api-check/container.py \
  --output artifacts/cm-005/container-new
```

The checker uses a new private artifact directory, generated test token and
loopback port, then removes only its uniquely named test container. It mounts
synthetic data read-only, checks all usage fixtures and twenty replacements,
attempts a refused write, inspects mount/env/image/log boundaries, measures idle
and 1-request/second traffic for sixty seconds each, then checks restart and clean
SIGTERM. The traffic phase is sixty times the default one-device request rate.
Host-to-VM publication visibility is bounded to three seconds and recorded.
Outputs are synthetic; no provider credentials or live source are accessed.

For the required LAN check, explicitly choose this Mac's local address with
`--lan <Mac-LAN-IP>`. This publishes the same test service temporarily on that
interface. It checks authenticated reachability through that address from the
Mac; it is not a physical CYD network/firmware test (CM-006). The normal launcher
continues to default to loopback. Use a new output directory for each run.

Resource evidence uses the local Docker Engine's cumulative CPU nanoseconds
and VM `ps` RSS for the sole API process, sampled every five seconds after a
five-second warmup. Container memory accounting is recorded separately from RSS.
Report the environment, image digest, sample interval and phase length. These
are preliminary API measurements; the combined 15-minute process budget,
Docker VM overhead comparison and device soak remain CM-010 requirements.

Repeat the small host benchmark in isolation:

```sh
GOCACHE="$PWD/artifacts/cm-005/go-cache" TMPDIR="$PWD/artifacts/cm-005/tmp" \
  go test ./internal/api -run '^$' -bench '^BenchmarkUsage$' -benchtime=1s -count=5
```

It includes reopening and reading the real synthetic snapshot, validation,
projection, authentication and JSON output using an in-memory HTTP recorder.
It reports wall time and allocations, not network latency or steady RSS.
