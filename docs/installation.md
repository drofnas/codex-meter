# Install and run on macOS

The native collector reads the signed-in Mac user's file-backed Codex credentials
and saves the current cycle plus 35 days of daily history. A Docker container serves only the public
snapshot. Two per-user LaunchAgents keep collection running and check the API
once a minute, including a coalesced check after waking. Run commands from this
checkout as the logged-in user, without `sudo`.

## Prerequisites and private configuration

Install Python 3.12–3.14, Go 1.26.8 or a newer patched release, and Docker Desktop. Start Docker Desktop and wait
for `docker info` to succeed. Enable Docker Desktop's own start-at-login option
if you want the API to return after login without opening Docker manually. The
installer does not change Docker settings. Complete sign-in through native Codex;
keychain-only credentials are not supported by this collector.

```sh
python3 scripts/meter.py addresses
python3 scripts/meter.py configure --bind-address 192.168.1.10
```

Replace the example address with the Mac's trusted Wi-Fi/LAN IPv4 address shown
by `addresses`. The command also lists VPN interfaces if present; choose the one
reachable by the CYD. Reserve that address in your router's DHCP settings. A
changed Mac address requires editing the bind address and firmware URL and
rebuilding/flashing. For a loopback-only trial, omit `--bind-address`. Nothing is
bound to all interfaces, and no router port forwarding is needed.

Configuration lives in ignored `.local/meter/.env` with permissions `600`. The
configure command creates a dedicated random API token there and refuses to
overwrite existing settings. Values use the same names as [.env.example](../.env.example):
literal `KEY=value`, no quotes, shell expansion, or interpolation. Edit the file
in your editor; do not `source` it. Lifecycle commands and jobs use this file
consistently, ignoring ambient `METER_*` overrides. Manual collector/API commands
retain their documented command-line/environment precedence.

The defaults collect every 60 seconds, become stale after 180 seconds, use a
10-second source timeout, and serve on port 8080. Blank timezone discovers the
Mac timezone; an explicit value must be an IANA name such as `America/Los_Angeles`.
Paths in the file resolve relative to that file. Data, private state and native
credential directories must not overlap. The lifecycle wrapper requires `750`
data permissions and your primary group so the non-root Docker API can read it;
state and installation directories use `700`. Unsafe existing permissions are
reported, never silently changed. Native credential permissions are never changed.

Copy [secrets.example.yaml](../secrets.example.yaml) to ignored `secrets.yaml`
if it does not already exist. Preserve existing Wi-Fi settings. Supply a **2.4 GHz**
SSID and password, `http://YOUR_MAC_LAN_IP:8080/v2/usage`, and the dedicated token
from `.local/meter/.env`. Keep the normal 60-second firmware polling and 10-second
HTTP timeout. Build and flash with the [existing firmware commands](../README.md#build-and-flash).
The token is separate from Codex credentials and must never be an OpenAI key.
HTTP is plaintext: use only a trusted private LAN, never expose this API publicly.

## Install and control

```sh
python3 scripts/meter.py doctor
python3 scripts/meter.py install
python3 scripts/meter.py start
python3 scripts/meter.py status
python3 scripts/meter.py stop
python3 scripts/meter.py start
```

`install` builds the native collector and Docker API image, then creates two
LaunchAgent files under `~/Library/LaunchAgents`. It leaves them stopped.
`start` enables both jobs, including subsequent login startup, and requires Docker
to be running at that moment. The API appears on the next automatic check; usually
within a few seconds at first start. Repeating start does not create a second
collector. The collector also holds OS locks on both data and state directories,
so a manual duplicate cannot publish concurrently.

`stop` disables login startup, unloads both jobs and removes only this
installation's Compose container/network. If Docker is unavailable, the native
jobs still stop; rerun stop when Docker returns to finish container cleanup.
Settings and history remain. Stop, edit private settings, then start to apply a
configuration change. After changing source or upgrading the toolchain, stop,
rerun install, then start. Keep this checkout and the selected Python interpreter
at their installed locations; uninstall before moving either.

`status` reports job registration, container state, snapshot publication age and
endpoint without printing quota values, tokens, or credentials. A loaded collector
may still be waiting for source access; use snapshot status/age to distinguish
that from current data. `doctor` checks settings, directory permissions and Docker
without opening native credentials. To inspect collector transition events in
the terminal, stop the jobs first and run:

```sh
.local/meter/meter-collector --env-file .local/meter/.env
```

Use Control-C, then `start` to restore supervision. Events are fixed codes such
as `auth_missing` or `source_timeout`; source bodies and credentials are omitted.

## Recovery and storage

- A collector crash is restarted by launchd, with a 30-second throttle. Existing
  source retries back off to at most five minutes. Each poll reopens the native
  credential file, so completing native Codex reauthentication restores collection
  automatically. The meter never signs in, refreshes credentials, or redeems resets.
- Sleep pauses collection. After wake, normal timers resume and the calendar job
  checks the authenticated API directly. A healthy check uses no Docker CLI child;
  an unavailable endpoint falls back to Docker inspection and Compose recovery.
  Unobserved consumption stays partial/unknown. Docker's restart
  policy handles container crashes; the calendar job recreates a removed/stopped
  container when Docker and the configured address are available again.
- Wi-Fi reconnects and API failures retain the LCD's last valid reading with
  increasing age. The firmware retries without a disconnect-driven reboot. A
  returned endpoint restores current readings automatically. Neither a reset
  countdown nor a disconnected screen invents a fresh quota observation.

Default runtime data is bounded: `.local/meter/data/usage.json` is at most 4 KiB.
The private `state/observation.json` and public `data/history.json` are each capped
at 64 KiB, with at most eight reset periods. `installation.key` is 32 bytes.
Atomic, synced replacement keeps the current observation and archive in one
private commit. Public usage and history files are independently replaced;
compare their publication timestamps when reading both endpoints.
Private state v4 also stores the highest observed usage so a percentage correction
and rebound cannot double-count daily consumption. Small reset-time changes (up
to five minutes from the fixed anchor) preserve approximate calendar-day totals.
The collector keeps owner-only `observation.pre-v4.json` and
`observation.previous.json` backups, each at most 64 KiB: the original upgraded
record and the last established ledger before its calendar was replaced.
OS locks and fixed temporary filenames do not accumulate debris. The reconciler
replaces a small `api-status.json` record. LaunchAgent stdout/stderr go to
`/dev/null`; Docker logs rotate at two 1 MiB files. Build caches remain separate.

The chart follows local calendar dates within the current reset period. A
Saturday 5 a.m. reset gives eight bars, Saturday through the following Saturday,
including the final five hours. Columns stay fixed as the current day advances.
A midnight reset gives seven bars; a daylight-saving transition can require nine.
`~` and stripes mean partial coverage, `?` means an unknown past value, and `0>`
means future zero. Missing observations are never backfilled as measured zero.
The reset display uses 12-hour time with the UTC offset in parentheses; portrait
puts the time and offset below the date. When resets are available, a small reload
icon and count appear on the right beneath the freshness state, top-aligned with
the main percentage. The earliest available reset expiration controls blue
(above seven days), yellow (four through seven days), or red (below four days).
Unknown expiration uses gray. Zero or unavailable count hides the indicator.
The next successful observation updates both count and color after a reset is
used elsewhere; this display never redeems resets.

Authenticated `GET /v2/history` exposes daily totals and coverage for the active
account over at least 35 days, retaining complete reset periods that overlap that
window. This is aggregate daily history, not raw polling records. Each period
includes its timezone and start/end instants. Historical snapshots retain their
last observed quota and publication timestamp; response-time freshness can be
stale without invalidating the stored daily evidence. The CYD polls `/v2/usage`
and downloads only the current period. Changing accounts clears the previous
account's retained history. A timezone change keeps completed periods in their
original timezone and preserves only matching intervals in the active period.

Upgrading from private state version 3 to version 4 preserves daily totals and
archives, initializes the correction baseline, and saves the exact original in
`observation.pre-v4.json`. Versions 1 and 2 use the same backup and retain the
observation and reset anchor, but start collecting calendar history forward.
Existing unobserved dates show `?`.
The v3-to-v4 private-state upgrade changes only the collector; an existing v2 API
and firmware need no update. When upgrading an older v1 public protocol
installation, upgrade the collector/API and firmware together and change
`meter_api_url` in firmware secrets to `/v2/usage`; v1 clients reject the new payload.
Before a rollback, stop collection and preserve the current state/history and binaries.
Restore the pre-upgrade state with the matching older collector/API and firmware;
that older version cannot read the v4 private archive. Keep the installation key
and credentials unchanged.

For the optional reset-expiration upgrade, stop collection and save a matching
private backup of the installation, state, published history, API image and
firmware. Install and start the upgraded API, and flash the upgraded firmware,
before restarting the upgraded collector. Earlier strict consumers reject the
new optional `resets_expire_at` field, and the earlier collector cannot read it
in private state. A rollback must restore the matching older components and
backed-up state together. Keep rollback records under ignored `artifacts/private/`.

## Uninstall and reinstall

```sh
python3 scripts/meter.py uninstall
# Later, resume the same cycle using retained settings and history:
python3 scripts/meter.py install
python3 scripts/meter.py start
```

Uninstall verifies ownership, stops the installation, and deletes only its two
LaunchAgent files, installed binary, installation record and reconciler status.
It preserves `.env`, `data/`, `state/`, firmware secrets, unrelated files, Docker
images and build cache. If Docker is down, restore it and retry uninstall; the
installation record remains available for cleanup. Never delete `installation.key`
while retaining the ledger: changing that key deliberately invalidates its scope
and baseline. For complete data erasure after uninstall, review and remove the
project's retained private files yourself. Do not remove or modify Codex's auth file.

For isolated trials, append `--directory artifacts/my-trial/service` to every
command. Configure can also take `--auth-file artifacts/my-trial/native/auth.json`
and `--port 18088`. Install accepts `--launch-agents-dir artifacts/my-trial/LaunchAgents`
for a repository-contained test: jobs are registered for the current login but
macOS will not discover that test directory on the next login. Each installation
has its own path-derived labels and Compose project. Use distinct ports and
separate data/state directories; do not run the older manual `scripts/api.py up`
stack on the same address/port. No command prunes unrelated Docker resources.
