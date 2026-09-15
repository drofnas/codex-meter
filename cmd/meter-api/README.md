# Private usage API

A single Go process serves the prepared [v2 calendar snapshot and retained history](../../contracts/v2/README.md).
It has no collector dependency, upstream client, credential store, shell, or
background worker. Each authorized usage request reopens `/data/usage.json`,
reads at most 4,097 bytes, validates the stored publication, then projects its
freshness and elapsed future slots. Observation and publication timestamps stay
unchanged. Missing data is unavailable; corrupt data is a sanitized 503.
`GET /v2/history` reads the separate `history.json` (64 KiB cap) and returns
35 days of daily aggregates across retained reset periods. It uses the same
bearer authentication and read-only, bounded-file validation. The device still
downloads only the current period. `/v1/usage` retains its original schema and
rejects v2 files; collector and firmware must upgrade together.
`GET /healthz` reports process liveness independently of snapshot availability.

For routine use, follow the [macOS installation guide](../../docs/installation.md)
for the combined collector/API lifecycle. The commands below serve manual API
development; stop that stack before using the installed service on the same port.

## Configure and start

Start Docker Desktop, run the native collector at least once to create its
usage directory, and copy the repository's `.env.example` to ignored `.env`.
Generate a dedicated token locally with `python3 -c 'import secrets; print(secrets.token_hex(32))'`
and put it in `METER_API_TOKEN`. The display uses that same dedicated token.
Keep the provider's credential file and private state outside the usage directory.

From the repository root:

```sh
python3 scripts/api.py check --env-file .env
python3 scripts/api.py up --env-file .env
python3 scripts/api.py status
curl --fail http://127.0.0.1:8080/healthz
python3 scripts/api.py down
```

`up` builds the image and starts the Compose project `codex-meter`. `down` and
`status` operate on that project's labels and remain usable when settings or data
are missing. Do not use this project name for unrelated containers. This helper
does not install a launch agent or start the native collector; `scripts/meter.py`
manages those jobs with a separate per-installation Compose project.

The default host publication is **127.0.0.1:8080**. For the physical display,
explicitly set `METER_API_BIND_ADDRESS` to the Mac's Wi-Fi/LAN IP. Wildcard and
multicast host publication are rejected by the launcher. The container's internal
listener is `0.0.0.0:8080`; Compose publishes only the selected host address.
Requests to `/v2/usage` and `/v2/history` require exactly `Authorization: Bearer <dedicated token>`.
Plain HTTP sends both the token and usage in cleartext on the trusted LAN.
Keep this endpoint off the internet and never use a Codex credential as its token.

Settings follow CLI > inherited environment > selected literal file > defaults.
Files are opt-in, bounded to 64 KiB, and contain literal `KEY=value` lines. There
is no shell execution or interpolation. Duplicate keys, unknown `METER_` settings,
empty required values and invalid ranges fail with `invalid_configuration`.
CLI names are the lowercase setting without `METER_`, with hyphens: for example
`--api-port 8081`, `--data-dir .local/data`, and `--timezone America/Los_Angeles`.
Paths resolve against the selected file's directory or the caller's working
directory; only a leading `~/` expands. Symlinks are resolved before checking
that data, state and credential directories are disjoint.

The launcher discovers an empty timezone from the Mac once, then passes the
resolved IANA name explicitly. The API never substitutes the container's UTC.
Timezone rules are embedded in its binary. The fallback timezone and stale
threshold apply only when the snapshot is missing; valid stored snapshots carry
their own settings. The shared poll/stale/timeout constraints are validated too.

## Runtime boundaries

The image is built from a pinned Go builder into `scratch` with just the static
API executable. The allowlist in `.dockerignore` keeps settings, private files,
collector source, Git metadata and artifacts out of every build layer. The final
image contains no shell or package manager and runs as UID 65532. The launcher
uses the host group by default (`--gid` overrides it) so it can read the
collector's 0750 data directory and 0640 snapshots. It does not chmod existing
files or provider credentials. Verify group access for a different host/runtime.

Compose mounts the **directory** read-only, refuses to create a missing source,
sets a read-only root filesystem, drops all capabilities and disallows privilege
gain. Only the dedicated token, resolved timezone, stale threshold, internal
listen settings and Go runtime limits enter the container. No complete host env
file is forwarded. `usage.json` must be a regular file; symlinks, directories and
FIFOs return `snapshot_unreadable`. No directory or log scan is performed.

Defaults are a 32 MiB container memory/swap limit, 24 MiB Go soft memory target,
0.25 CPU cap, 64 tasks and two Go processors. The cap is a ceiling, not expected
CPU use. Launcher overrides are `--memory-mib` (16–48), `--cpu-limit` (0.01–1),
`--pids-limit` (32–128) and `--gid`. Limits remain below the project's intended
small-service scale; the combined collector/API budget still needs CM-010's
15-minute measurement. Docker Desktop VM overhead is separate.

Header/read, write and idle timeouts are 5s, 10s and 30s, with an 8 KiB HTTP header
setting. There are no access logs or raw error logs; startup errors are fixed
codes. Docker logs have bounded rotation. SIGINT/SIGTERM permits five seconds
for active requests, then closes remaining connections. `unless-stopped`
restarts the service after a process failure or daemon restart; restarting it
never touches collector state. A broken snapshot does not restart the process.

Docker Desktop's host-to-VM sharing can briefly delay visibility of a file
replacement. The container check measures this interval and requires convergence
within three seconds; every interim body must still be contract-valid. The API
itself holds no snapshot cache. Physical CYD network/reconnect checks are CM-006.

## Validation

Use the [API validation tools](../../tools/api-check/README.md). The HTTP table
and full format/semantic rules remain in the normative contract. Malformed HTTP
framing rejected by Go before dispatch is a transport failure, not a JSON route
response.

The standalone binary is an internal container entrypoint. It accepts only its
resolved runtime settings through environment or flags (`--bind-address`,
`--port`, `--data-dir`, `--timezone`, `--stale-seconds`, `--token`); it does not
load host settings files or discover a timezone. Use the launcher for host
configuration and path-boundary validation.

Packaging follows Docker's [read-only directory mount documentation](https://docs.docker.com/engine/storage/bind-mounts/)
and [Compose service configuration](https://docs.docker.com/reference/compose-file/services/).
The builder uses a supported Go release from the [official release history](https://go.dev/doc/devel/release).
