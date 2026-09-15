# Integrated acceptance checks

These tools operate the real native collector, local Docker API and USB-connected
CYD. Run from the repository root with the ESPHome Python (pyserial required).
Native credentials are opened read-only by the collector and independent probe;
all raw quota, scope, frame and process evidence stays under ignored artifacts.
No provider response body, bearer token, Wi-Fi configuration or host-wide command
inventory is retained by these observers.

Use `scripts/meter.py` to configure/install the real service with the same existing
LAN endpoint/token as the firmware. For repository-contained validation, install
with `--launch-agents-dir .local/meter/LaunchAgents`. Those jobs last for the current
GUI login; they do not provide login startup after logging out. Leave the normal
60-second collection/device poll and 10-second timeout settings in place.

```sh
# Standard regression gates (use the documented jsonschema environment):
.venv/bin/python tools/api-check/check.py --output artifacts/cm-010/backend
python3 tools/firmware-check/check.py
.venv/bin/python tools/display-check/check.py
# Use ESPHome Python for acceptance tool tests:
python3 -m unittest discover -s tools/acceptance-check
```

Build the independent probe from its own Go module under `tools/collector-probe`,
placing its binary in `artifacts/cm-010/collector-probe`. Use the existing physical
network/display fixture workflows, then build and flash production firmware.
Stop fixture listeners before starting the actual API on the same endpoint.

```sh
# Start against the prepared stopped installation and capture startup/live data:
python3 tools/acceptance-check/observe.py --start --reset --seconds 1080 \
  --probe-binary artifacts/cm-010/collector-probe --output artifacts/cm-010/live
# In a separate terminal, once live API and device readings are available:
python3 tools/acceptance-check/resources.py --output artifacts/cm-010/resources
python3 tools/acceptance-check/verify.py artifacts/cm-010/live
# While a live observer owns serial, hold an actual API outage and restore it:
python3 tools/acceptance-check/recovery.py --output artifacts/cm-010/live-recovery
python3 tools/acceptance-check/verify.py artifacts/cm-010/live \
  --recovery artifacts/cm-010/live-recovery
# After the fault matrix and any planned Mac sleep/wake test:
python3 tools/acceptance-check/observe.py --seconds 86400 \
  --probe-binary artifacts/cm-010/collector-probe --output artifacts/cm-010/soak
python3 tools/acceptance-check/verify.py artifacts/cm-010/soak --soak
```

Use a new output directory for each run. The observation writes incremental
`status.json`, filtered `serial.jsonl`, selected `host.jsonl`, private `usage.jsonl`
and independent `source.jsonl`. A completed recording is not by itself a passed
story: run the verifier, inspect memory trends, and obtain actual LCD/settings
and real sleep/wake evidence. The soak verifier refuses a shortened duration,
missing serial coverage, reboot/panic, mismatching display facts, or missing live
comparisons. It reports unmatched observations rather than fabricating evidence.
It also compares daily labels, coverage and rounded values (including future-day
expiry), and requires a fresh displayed observation with measured daily history.
A healthy gauge and continuous uptime alone cannot pass with all days unknown.

The resource window is at least 900 seconds after a 30-second warmup. It samples
native process-tree RSS plus Linux API RSS every five seconds, with faster
sampling during the short periodic job. Linux RSS comes from Docker Engine `top`;
cgroup CPU counters account for the API. During this one window, the harness
unloads only the launchd API scheduler and invokes the unchanged `_api` command
at its normal 60-second cadence under `/usr/bin/time -l`. This accounts for CPU
of every reaped short-lived child that five-second `ps` sampling would miss.
It restores the original launchd calendar job in `finally`. Collector supervision
and the actual container remain active. A crash of the measurement harness itself
requires restoring the prepared API LaunchAgent before continuing acceptance.
Control-C and SIGTERM take the normal cleanup path. Keep resource measurement
and injected outages separate: restarts invalidate cumulative CPU comparisons.

The recovery check removes only the selected installation's API container and
suspends only its calendar job for 100 seconds. Its `finally` restores the job;
normal reconciliation must then recreate the container and return fresh data.
Inspect the concurrent serial evidence for stale retention and actual LCD
recovery. An HTTP recovery alone does not prove a displayed recovery.

Instrumentation is excluded from the application budget; the tiny `time` process
is conservatively included in sampled RSS. Results identify sampled maxima, not
continuous peaks, and include transient jobs. Startup sampling and shared Docker
host/VM process deltas are recorded separately. Do not compare host cgroup anon
memory to RSS or exclude children merely because they exit between samples.

The physical fault verifier uses serial stream order for tied capture timestamps:
a busy-state line printed before a poll result cannot be attributed to that
result. Retention assertions still apply to every subsequent state line.
