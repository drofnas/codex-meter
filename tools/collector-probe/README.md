# Read-only Codex collector probe

This CM-001 spike verifies live weekly quota access and measures the cost of a
small native helper. It does not yet publish the firmware snapshot, maintain
history, install a service, or run the Docker API.

Run from the repository root with Go 1.26.8 or a newer patched release;
no third-party Go modules are required.

```sh
mkdir -p artifacts/cm-001/go-cache artifacts/cm-001/tmp
env GOCACHE="$PWD/artifacts/cm-001/go-cache" \
    GOTMPDIR="$PWD/artifacts/cm-001/tmp" TMPDIR="$PWD/artifacts/cm-001/tmp" \
    GOTOOLCHAIN=local go -C tools/collector-probe test -race -count=1 ./...
env GOCACHE="$PWD/artifacts/cm-001/go-cache" \
    GOTMPDIR="$PWD/artifacts/cm-001/tmp" TMPDIR="$PWD/artifacts/cm-001/tmp" \
    GOTOOLCHAIN=local go -C tools/collector-probe vet ./...
env GOCACHE="$PWD/artifacts/cm-001/go-cache" \
    GOTMPDIR="$PWD/artifacts/cm-001/tmp" TMPDIR="$PWD/artifacts/cm-001/tmp" \
    GOTOOLCHAIN=local go -C tools/collector-probe build -trimpath \
    -ldflags='-s -w' -o ../../artifacts/cm-001/collector-probe .

# One account-usage read; output contains only the projected quota fields.
artifacts/cm-001/collector-probe

# Bounded polling, without background installation.
artifacts/cm-001/collector-probe -duration 5m -interval 60s

# 30-second warm-up and at least 15 minutes of steady sampling on macOS.
python3 tools/collector-probe/measure.py \
    --binary artifacts/cm-001/collector-probe \
    --output artifacts/cm-001/measurement --seconds 900 --warmup 30
```

`gofmt -l tools/collector-probe/*.go` should produce no paths. The measurement
command records sampled RSS and cumulative CPU time for the probe and any
observed descendants. The Python harness and transient `ps` processes are test
instrumentation, excluded from application measurements. The runtime launches
no subprocesses. Review both resource totals and successful/failed poll counts;
the harness emits measurements rather than certifying the entire application.

## Authentication and network boundary

By default the probe opens `~/.codex/auth.json` read-only on every poll; use
`-auth-file /absolute/path/auth.json` for a different file. This spike supports
the current file-backed ChatGPT login format. It does not read Keychain, perform
login, use a refresh token, or change native credentials. A missing, malformed,
expired, or nearly expired token yields an unavailable code before networking.
The local JWT expiry check does not verify a signature; the service verifies
authentication. Codex remains responsible for renewal and sign-in. Updated
credentials are read on the next poll; persistent failures require repairing
the native Codex sign-in, not giving the probe refresh ownership.

The sole network operation is an HTTPS GET to the fixed Codex usage endpoint
on `chatgpt.com`. TLS verification remains enabled; redirects and environment
proxies are not followed. No account-control, reset, inference, or refresh call
exists. Requests have a 10-second deadline and responses have a 256 KiB cap.
Provider response bodies and transport errors are never logged. Only projected
quota fields leave the parser; account identity, email, credentials, credit
detail records, and prompts are absent from output.

The internal HTTP endpoint is not a documented public API contract. Its address
was found in the installed Codex binary and its current response verified against
the desktop account read. The documented [app-server interface](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)
is an alternative; the running desktop's private socket was not assumed to be
that interface. Keep the adapter replaceable and fail unavailable when the
expected weekly data is missing. A spawned app-server adapter was not measured.

## Data meaning and fixture scope

The live adapter selects the endpoint's general-Codex `rate_limit` object,
ignoring `additional_rate_limits`. It finds exactly one 604,800-second window
in primary or secondary position. Values must be finite numbers from 0 through
100 with a positive integral reset timestamp.

Fixtures use synthetic values and identifiers with the observed/documented
structure. They cover primary/secondary placement, unrelated model buckets,
missing/ambiguous windows, and zero/unknown reset count. Optional
`available_count` is independent of monetary credits and detail-list length.

Each success reports the request's observation time. An error emits only an
unavailable status and a fixed error code. These lines are private measurement
output, **not** the future versioned firmware contract. The production collector
must add last-known state, atomic publication, backoff, and history separately.

## Reset and history findings

The nonzero general-Codex weekly reset stayed stable in the initial paired
reads. An unused alternate-model window's reset time moved between reads;
timestamp change alone is therefore unsafe as universal reset evidence.
This probe does not cause resets or infer a new cycle when wall time passes.

For the contract story, a later live observation spanning the previous boundary
with a compatible new window can establish a scheduled transition. An early
percentage drop, changed allowance, or unused-window drift needs explicit
evidence or an unknown/partial history state. Do not label ambiguous negative
deltas as consumption, fabricate a daily split across observation gaps, or claim
that a manual reset was observed during this spike. Exact recognition rules
remain a CM-002 contract decision, backed by conservative fixtures.

Keep live measurements under ignored `artifacts/`. Public examples and fixtures
must use synthetic observations; see [SECURITY.md](../../SECURITY.md).
