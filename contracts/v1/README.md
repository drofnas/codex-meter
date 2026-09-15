# Codex usage meter contract v1

This defines the original v1 format. The collector and CYD use
[v2](../v2/README.md), which inherits the shared rules below. The API retains v1
validation for snapshots matching that version. Schemas, fixtures and the
development checker remain as protocol regression coverage.

Validate with both [usage.schema.json](usage.schema.json) and the cross-field
rules in [validate.py](../../tests/contracts/validate.py). JSON Schema alone
cannot check arithmetic, timezone conversion or freshness. Structural schemas use
[JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12). The development
validator uses [jsonschema](https://pypi.org/project/jsonschema/), not a runtime
dependency. See [checker instructions](../../tests/README.md).

## Wire representation

UTF-8 JSON object, exactly one document, no duplicate keys, NaN, Infinity or
overflowing numbers. All defined keys are required; extra keys are rejected.
An incompatible shape requires a new version and route. A receiver must reject
an unsupported version, even if it recognizes other fields. Numeric booleans
are invalid. Integer fields permit mathematically integral JSON numbers.

The raw, uncompressed body, including whitespace, is at most **4,096 bytes**.
Writers use compact JSON. Readers read at most 4,097 bytes to detect overflow.
Firmware sets its response capture limit to 4,096 and rejects truncation or
overflow; a C-string implementation reserves a separate trailing NUL byte.
Chunked bodies must obey the same accumulated cap. Do not accept compressed
responses. Never silently truncate a snapshot to fit.

| Field | Meaning |
| --- | --- |
| `version` | Integer 1. |
| `scope` | 64 lowercase hex digits identifying this installation/account pair, or null without an observation. HMAC-SHA256 of the native account ID with a private installation key. Never the raw account ID. |
| `bucket`, `window_minutes` | Exactly `codex` and 10080. Select the general weekly bucket by identity and duration; it may occupy either source window. Never sum task snapshots or substitute Spark. |
| `observed_at` | Unix seconds when the source reading was obtained, or null. If a source supplies its own valid observation timestamp, preserve it. For the verified HTTP adapter, stamp response completion, not a later API read. |
| `updated_at` | Unix seconds of native snapshot publication, or null for a synthesized missing-file response. A failed collection can update this without changing `observed_at`. |
| `as_of` | Unix seconds at which freshness and future-slot presentation were evaluated. Stored publication uses `as_of = updated_at`; API responses use current time. |
| `age_seconds` | `max(0, as_of - observed_at)`, or null without an observation. |
| `stale_after_seconds` | Integer 30–3600, default 180; chosen by the collector and honored by API/device. |
| `status`, `reason`, `source_error` | Derived status plus a bounded reason/code, never an upstream error body. Rules below. |
| `remaining_percent` | Finite 0–100, nullable. Normalize valid nonnegative used percent as `clamp(100 - used, 0, 100)`. Missing, null, negative or non-finite source values fail collection; never turn them into 100% remaining. |
| `reset_at`, `reset_local` | Authoritative reset epoch and its local `YYYY-MM-DD HH:MM ±HH:MM` rendering, both null without an observation. Do not predict the next reset by adding a week on a timer. |
| `timezone` | Valid IANA zone key, maximum 64 characters; `UTC` is valid. Chosen on the Mac. |
| `cycle` | `start_at = reset_at - 604800`, `end_at = reset_at`, and state `observed`, `confirmed`, `ambiguous`, or entirely null/`unknown` without an observation. These are nominal quota-window bounds. |
| `days` | Exactly seven ascending, consecutive 86,400-second slots; no calendar-Monday rebasing. Fields described below. |
| `resets_available` | Optional source-supported earned reset count, null or integer 0–2147483647. Null is not zero. Never derive it from credits or a details-array length. |

Epochs are integral seconds in 1–4102444800 (through 2100-01-01 UTC). An accepted
reading must fall in its nominal cycle, allowing at most five seconds before the
start for clock skew, and strictly before its reset. An expired/inconsistent
source reading is a collection failure. Publication cannot precede observation.
Scope is stable across restarts; a changed account or lost/rotated installation
key starts unknown history. Keep the key and internal account identity in private
state outside the shared directory. Neither is available to the container.

Reset count is optional even when quota collection succeeds. An absent, malformed
or unsupported count becomes null on that successful observation. A whole-source
failure retains the previous count with the same stale status as its quota. A
count change is not evidence that a reset was redeemed. No component redeems one.

## Freshness and failure

Evaluate these rules in order:

1. No observation: `unavailable / no_observation`, age null. Quota, scope, reset,
   count and cycle instants are null; seven unknown slots have null starts,
   labels and values. A first failed attempt may have `updated_at` and a source
   error; a missing file has neither. Timezone and threshold still come from
   configuration.
2. `as_of + 5 < max(observed_at, updated_at)`: `stale / clock_error`.
3. Non-null `source_error`: `stale / source_error`, even if younger than 180s.
4. `as_of >= reset_at`: `stale / reset_due`.
5. `age_seconds >= stale_after_seconds`: `stale / too_old` (180s is stale).
6. Otherwise `ok`, reason null.

Source errors are only `auth_missing`, `auth_failed`, `source_timeout`,
`source_unavailable`, `source_invalid`. Failed attempts retain last-known quota,
cycle, history, count and observation time, and publish the fixed error code.
A successful reading clears it. API-response time never changes observation or
publication time. Healthy HTTP delivery cannot make a failed collector fresh.

The API first validates the stored snapshot against its stored `as_of` using
`validate_snapshot()` semantics: a non-null publication time and `as_of` equal
to that time. A synthesized unavailable response is never a stored publication.
It then
projects `as_of`, age/status/reason and elapsed future slots. A formerly future
slot that has opened without observations becomes unknown/null. No history is
created by projection. For a backwards wall clock, use
`max(as_of, observed_at, updated_at)`
to evaluate day coverage, preserving facts from before the jump. The reading is
still marked with the clock error. [project()](../../tests/contracts/validate.py)
is an executable reference for this operation.

The CYD preserves the last accepted model on HTTP, parsing or validation failure,
shows the connection/error condition, and advances age with a monotonic timer.
On repeated readings with the same observation, its age must not decrease:
use the larger of received age and locally elapsed age. A newer observation can
replace it. A changed scope discards the old account's display/history. Within
one scope ignore responses with older observation or publication times. At equal
times allow API projection changes only; conflicting source/history fields are
invalid. Reconnection clears transport failure only after a valid response.

## Daily history and reset decisions

Each day has `start_at`, `label`, `used_delta_pp`, `coverage`. Labels are the
local weekday of that slot's start: **M, T, W, Th, F, Sa, Su**. The Mac supplies
labels and the reset string, so ESP32 does not need an IANA timezone database.
Changing timezone reformats labels/reset only; it does not repartition history.
Across DST, slot duration remains exactly 86,400 seconds. Labels can repeat or
skip a weekday when local time shifts; do not require seven unique labels.

| Coverage | Value and interpretation |
| --- | --- |
| `future` | Start strictly after evaluation time; value 0, a placeholder for time not reached. |
| `unknown` | No defensible measured amount; value null. Past and current slots may be unknown. |
| `partial` | Observed delta is known, but coverage is incomplete or slot is still open; value 0–100. |
| `complete` | Closed slot, continuously covered with attributable endpoints; value 0–100. |

`used_delta_pp` is observed percentage-point consumption, not tokens or a share of
the week's total. The sum of all known values must be at most 100. Fresh gauge
readings can coexist with unknown/partial history. Never allocate pre-install
usage to the current day. Initial history is unknown for elapsed slots and zero
for future slots. Zero becomes measured only after compatible observations show
no increase over a covered interval.

The original history engine follows these transition rules:

- Deduplicate by scope, bucket, duration and source observation timestamp.
  Identical repeats contribute nothing. Ignore older observations. Equal-time
  conflicts invalidate the interval and mark history ambiguous; never add both.
- A compatible pair has the same cycle identity, increasing timestamps, no
  detected allowance adjustment, and a gap at most twice the configured poll
  interval (120s by default). Same-slot positive used-percent deltas are added
  once. Same-slot zero deltas establish measured zero coverage.
  Attribute the interval `[previous timestamp, new timestamp)`; a sample exactly
  on a slot's end can close that slot without crossing into the next one.
- A positive delta crossing a slot boundary cannot be assigned to either slot;
  discard that interval's delta, preserving already-known amounts as partial and
  otherwise unknown. A zero delta across a boundary can establish zero coverage
  on both sides. Do not interpolate a positive delta by elapsed time.
- Larger gaps, restart without a valid saved baseline, or invalid observations
  break coverage. Deltas across the gap are not assigned. Preserve known portions
  as partial; otherwise unknown. A complete closed slot requires coverage from
  its start through its end without unknown positive boundary deltas or gaps.
- Persist the bounded active-cycle ledger and baseline atomically. A late sample
  may finish evidence for a just-closed slot, but cannot rewrite a discarded gap.
- The first accepted source reset gives `observed` cycle state. A scheduled new
  cycle is confirmed only with a new valid source observation at/after the old
  end, a strictly later source reset, and new nominal start between the old end
  and the observation (allowing five seconds of skew). This also handles missing
  entire cycles: start the observed current cycle with unknown earlier slots;
  do not fabricate intervening weeks. Reset-time passage alone confirms nothing.
  The old end used for this comparison is the last stable observed/confirmed
  cycle's end, retained privately through ambiguity; a drifting ambiguous reset
  must not replace that transition anchor.
- A used-percent decrease, an early reset-time change, incompatible reset drift
  or detected allowance change yields `ambiguous`. Show fresh authoritative
  gauge/reset data, but elapsed chart values become unknown; future values remain
  zero. Preserve the old private ledger until a confirmed transition, not an
  ever-growing archive. Do not accumulate deltas in an ambiguous cycle. If the
  source exposes no allowance metadata, undetectable changes cannot be resolved;
  this chart is explicitly observed quota movement, not billing-grade accounting.
- Early manual resets require explicit trusted reset-event evidence to be
  confirmed. The current HTTP adapter has none; a count decrease or quota drop
  alone is insufficient. Remain ambiguous until a later scheduled transition can
  be confirmed. The count wishlist does not block the gauge or this policy.

Examples for downstream engine tests (all percentages are used, not remaining):

| Input sequence | Required result |
| --- | --- |
| Same slot: 10 at t, 12 at t+60, exact duplicate | Add 2pp once; partial. |
| Same timestamp: 10 then 12 | Ambiguous chart; no 2pp attribution. |
| 10 at t, 12 at t+180 with 60s polling | Gap; no 2pp attribution. |
| 10 at boundary−30, 12 at boundary+30 | Both affected slots partial/unknown; no split. |
| 10 at boundary−30, 10 at boundary+30 | Zero coverage on both sides, not proof of a whole day. |
| 50 then 5 before the old reset | Fresh 95% remaining, ambiguous history, no confirmed reset. |
| Clock passes reset, no successful source call | Old cycle retained, reset_due/stale. |
| Source call after old reset with compatible later reset | Confirm new cycle; unknown elapsed history. |

## New HTTP endpoints

The private read-only API binds a configured host address. Default publication is
loopback; a user explicitly selects the Mac's LAN IP for the CYD. HTTP is plaintext
on that trusted LAN, including the dedicated bearer token. Do not expose the
service to the internet or reuse Codex credentials as its token.

| Request/result | Status and body |
| --- | --- |
| `GET /v1/usage`, correct `Authorization: Bearer <token>` | 200, valid usage envelope, including stale/unavailable. |
| Missing/wrong/multiple/malformed usage authorization | 401, `unauthorized`, `WWW-Authenticate: Bearer`. Compare tokens in constant time. |
| Missing snapshot | 200, synthesized unavailable envelope. |
| Invalid JSON, fields, arithmetic or timezone | 503, `snapshot_invalid`. |
| Stored snapshot over 4096 bytes | 503, `snapshot_oversized`. |
| Unsupported stored version | 503, `snapshot_version`. |
| Other file read/permission failure | 503, `snapshot_unreadable`. |
| `GET /healthz` | Unauthenticated 200, `{"version":1,"status":"ok"}`. Process liveness only; no account/source information. |
| Unknown path | 404, `not_found`. |
| Non-GET method on a known path, including HEAD | 405, `method_not_allowed`, `Allow: GET`. |
| Query string or request body on a known GET route | 400, `bad_request`. |

Precedence: exact path, method, authentication (usage route), query/body checks,
then snapshot read. Error bodies are exactly `{"version":1,"error":"<code>"}`,
at most 256 bytes, per [error schema](error.schema.json). Health uses the
[health schema](health.schema.json). Do not return raw source errors, paths,
account IDs or tokens. Do not log Authorization headers or snapshot bodies.
Every response is JSON with `Cache-Control: no-store`; no 304 freshness shortcut.

The API reads one bounded `/data/usage.json` per request; it never contacts the
account service, spawns a collector or scans logs. Mount the **directory** read
only so atomic replacement remains visible. Native publication writes a complete
temporary file in that directory, then renames it over `usage.json`. Keep private
ledger/key files outside the mount. Interrupted writes leave the last valid file.

Native source and device HTTP total timeouts default to 10s; no overlapping
requests, redirects or unbounded retries. Default collector/device polls are 60s.
API defaults: 5s header/read timeout, 10s write timeout, 30s idle timeout, 8 KiB
header limit. Transient retry waits stay between the configured interval and
300s; reset after success. Polling/backoff must retain truthful age throughout.

## Local configuration

Copy [.env.example](../../.env.example) and
[secrets.example.yaml](../../secrets.example.yaml) to ignored local equivalents.
These templates deliberately require a private token and Wi-Fi values before
running the application. The CYD network client consumes its ESPHome secrets.
It currently supports
literal IPv4 LAN endpoints, avoiding a separate DNS operation outside the total
request deadline.

Precedence: explicit CLI option > inherited environment > selected `.env` file >
default. An explicitly empty required value is invalid, not a fallback. Parse
env files as literal `KEY=value` lines with blank/comment lines permitted; no
shell execution, interpolation or variable expansion. Reject unknown `METER_`
keys and duplicate keys in a file. Firmware uses ESPHome `!secret`/substitutions
from its local `secrets.yaml`; there is no runtime environment on the CYD.

| Setting | Default and validation |
| --- | --- |
| `METER_AUTH_FILE` | `~/.codex/auth.json`; native collector only, read only. |
| `METER_DATA_DIR` | `.local/data`; usage-only shared directory. |
| `METER_STATE_DIR` | `.local/state`; private bounded ledger and installation key, never mounted. |
| `METER_TIMEZONE` | Empty: discover the Mac's IANA zone once on startup. If unavailable, require an explicit valid zone; never silently use the container's UTC zone. |
| `METER_COLLECTION_SECONDS` | 60, integer 10–300. |
| `METER_STALE_SECONDS` | 180, integer 30–3600, at least twice collection interval. |
| `METER_SOURCE_TIMEOUT_SECONDS` | 10, integer 1–30, no greater than collection interval. |
| `METER_API_BIND_ADDRESS` | `127.0.0.1`; literal local IP, explicit LAN binding required for device access. |
| `METER_API_PORT` | 8080, integer 1024–65535. |
| `METER_API_TOKEN` | Required 64 lowercase hex digits representing 32 random bytes. Dedicated meter secret, same value in firmware. |
| `wifi_ssid`, `wifi_password` | Local Wi-Fi credentials, no real values in examples or logs. |
| `meter_api_url` | Required HTTP URL with explicit host/port and path `/v1/usage`, no userinfo/query/fragment. Example uses reserved documentation IP 192.0.2.1. |
| `meter_api_token` | Same dedicated token as API; not a Codex credential. |
| `meter_poll_interval`, `meter_http_timeout` | Default `60s`, `10s`; integer seconds, poll 10–300, timeout 1–30 and no greater than poll. Keep stale threshold at least twice device polling too. |

Native path values expand a leading `~/` only. Relative paths resolve against
the selected env file's parent, or the working directory if no env file is
selected; resolve once to absolute paths before launchd/Compose handoff. The data
and state directories must be disjoint, and neither may contain/be inside the
credential directory. Refuse a symlink that defeats these boundaries. Do not
chmod or rewrite the provider credential file. Create private state owner-only;
grant the container's selected UID/GID read access only to usage data.

Container wiring passes only its dedicated token, fallback timezone,
stale threshold and listen settings; never forward the entire host env file.
Internal container listen can be `0.0.0.0:8080`, while Docker host publication
uses the explicit `METER_API_BIND_ADDRESS`/port. The sole bind mount is
`METER_DATA_DIR:/data:ro`. Firmware build artifacts contain local secrets and
remain ignored. `.env`, `.env.*`, `secrets.yaml`, other local secrets, `.local/`
and build artifacts are excluded; the two safe examples remain tracked.

The 120-second freshness objective is measured with the 60-second defaults;
slower user overrides change that expectation. This contract adds no daemon or
runtime dependency. The combined native helper/API process-tree budget remains
below 64 MiB steady RSS and below 1% of one core over 15 minutes. Measure the
actual implementation on the target host; fixture size alone proves
only the response budget.
