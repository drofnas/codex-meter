# Calendar usage and retained history (v2)

`GET /v2/usage` returns the current reset period. It uses the v1 quota,
identity, ordering, authentication, and freshness rules, with the following
incompatible calendar-day changes. `usage.schema.json` plus these semantics are
normative. The v1 route/schema retain their original meaning; each route rejects
a snapshot whose version does not match. Only one current snapshot is published.

- `version` is 2. Device responses remain capped at **4096 bytes** (including
  whitespace); the client retains its separate overflow-detection byte.
- `cycle.start_at = reset_at - 604800`, `cycle.end_at = reset_at`. Dates never roll
  forward just because the current date changes, and the clock cannot confirm a
  quota reset. The source confirms each new period.
- `days` contains **7–9 contiguous nonempty intervals**, clipped to that period.
  Each has `start_at`, `end_at`, `label`, `used_delta_pp`, and `coverage`. The host
  splits at midnight in `timezone`, accounting for daylight-saving changes.
  The first interval starts at the quota-period start and the last ends exactly
  at reset. Endpoints are exclusive. A midnight reset adds no zero-duration day.
- Labels remain `M`, `T`, `W`, `Th`, `F`, `Sa`, `Su` and describe the local date at
  each interval's start. Saturday 5 a.m. through next Saturday 5 a.m. therefore
  includes both Saturdays: 19 hours in the first and 5 in the last.
- Missing cycle bounds require `days: []`. Known cycle bounds require all its
  calendar intervals, even if all previous values are unknown. Unknown past
  values are `null` and rendered `?`; future values remain `0` with coverage
  `future`. Measured zero is distinct from either case.
- A day's usage is percentage points of the weekly allowance, never prorated by
  its duration. Partial first/last dates can have complete coverage if their
  entire in-period interval was measured. `complete` is legal only after that
  interval closes. A positive delta spanning an unobserved calendar boundary is
  not attributed to either date. Existing gap and ambiguity protections remain.
- `reset_local` remains `YYYY-MM-DD HH:MM ±HH:MM`, derived from `reset_at` and the
  IANA timezone. The screen formats it as `YYYY-MM-DD h:mm AM/PM (offset)`.
  Firmware checks interval continuity and reset epoch/text consistency; the host
  owns IANA midnight boundaries and weekday correctness.

## Reset variation and forward recovery

These rules supersede v1's requirement to remain ambiguous until a scheduled
transition. The wire format and the meaning of unknown/partial/complete do not
change.

- Source resets within **two seconds** of the fixed private accounting anchor are
  compatible. Preserve the exact source reset in public bounds and reset text.
  Only exactly matching calendar intervals expose retained totals; changed edge
  intervals remain unknown. Returning to the anchor restores those matching
  intervals. Compare to the fixed anchor to prevent cumulative timestamp drift.
- Attribute positive deltas only when the entire observation interval is inside
  both source periods and the accounting period. Zero deltas may be clipped to
  their intersection. Existing gap and midnight attribution rules still apply.
- A larger reset change, quota decrease, conflicting same-time observation or
  excessive accumulated quota makes the uncertain history ambiguous. A newer
  nondecreasing reading with exactly the same reset as its predecessor, no more
  than twice the configured collection interval apart, establishes a fresh
  `observed` baseline. Discard the interval leading into recovery and old active
  totals. Subsequent compatible readings accumulate partial amounts. Replays,
  long gaps, continued reset movement and decreasing readings do not recover it.
- Recovery works for persisted ambiguity, including migrated legacy state. It
  does not confirm an early reset or backfill unknown consumption. Scheduled
  transitions retain their existing confirmation requirements using the current
  accounting anchor.
- Retain completed periods and replace the still-open period on each publication.
  Timestamp variations and recovery do not add overlapping active snapshots.

## Retained data

Authenticated `GET /v2/history` returns `history.schema.json`: `version`, `scope`,
`updated_at`, `as_of`, `retention_seconds` (3024000, or 35 days), and `cycles`.
Each cycle is a v2 usage snapshot containing its daily aggregates, timezone,
period bounds, last quota observation, and coverage. Entries are ordered by reset
instant without duplicates and belong to the envelope's account scope. The
maximum is eight periods and **65536 bytes**, separate from the CYD response cap.
The route accepts no query parameters, request body, compression, or writes, and
uses the same dedicated bearer authentication and no-store headers as usage.

Retain whole observed periods whose end is later than `as_of - 35 days`. This
preserves every retained day's full evidence rather than chopping an aggregate at
the retention boundary. Collection starts forward from installation; absent
periods and unknown days are not reconstructed. Collection outages can leave gaps.
Account changes clear prior-account history. Completed periods keep their original
timezone; a timezone change only repartitions the active period, retaining totals
for exactly matching intervals. This is daily aggregate history, not raw samples.

At response time, the API updates each snapshot's `as_of`, freshness, age, and
newly opened future slots. Completed periods can be stale while their historical
daily values remain valid. Reading history never fetches provider data or writes
storage. No history file produces an empty list, null scope/publication time,
and the current `as_of`. Invalid history produces a bounded v2 error and cannot
fall through to the private state or credentials.

The collector atomically commits the current observation and retained snapshots
in private state v3 (64 KiB cap), then independently replaces public `history.json`
and `usage.json`. Both public files expose publication timestamps; they can
briefly reflect adjacent publications after interruption. Restart reloads the
private commit to avoid duplicate attribution. The old v1/v2 private record gets
one owner-only `observation.pre-v3.json` backup before forward-only migration.
Failure to create that backup blocks publication and preserves the original.

## Verification

Run the v1 fixture suite unchanged and the v2 suite separately:

```sh
.venv/bin/python tests/contracts/validate.py
.venv/bin/python tests/contracts/validate.py --version 2
.venv/bin/python tests/desktop/check.py
```

`tests/contracts/generate_v2.py` reproducibly builds synthetic calendar fixtures. Go tests verify
retention with 60 simulated dates and restart/publication failures; those tests do
not require waiting for real dates or reading the user's credentials.

## Optional earned-reset indicator (CM-009)

`resets_available` remains the authoritative nullable count, independent of
monetary credits. The optional `resets_expire_at` field is the earliest finite
expiration among **all** available reset credits, in Unix seconds. It is legal
only for a positive count and an expiration later than `observed_at`. Missing or
null means expiration is unknown or all credits are non-expiring. Existing v2
snapshots without this field remain valid; v1 remains unchanged. Public snapshots
and history never contain reset-credit IDs, descriptions, or profile metadata.

The native collector reads the existing fixed usage endpoint, then, only for a
positive count, GETs the fixed `/backend-api/wham/rate-limit-reset-credits` endpoint
with a two-second sub-deadline inside the existing poll timeout. Both reads use
the same account credentials, bounded response size, no redirects/proxy, and
pooled connection. The later valid detail-response count supersedes the usage
count. Missing/capped/inconsistent/duplicate/unsupported details do not establish
an earliest expiration. A detail failure preserves the good usage count and
weekly reading with unknown expiration; it never reuses an old expiration as
fresh. There are at most two GETs per default 60-second successful poll and no
redemption request, control route, or credential refresh.

The display shows only a reload icon and exact count on the right below FRESH/STALE,
top-aligned with the main weekly percentage, hidden for zero/unavailable count.
It uses blue above seven days,
yellow from four through seven days inclusive, and red below four days. These are
elapsed 24-hour days to the earliest expiration; exactly seven days is yellow.
Unknown expiration uses neutral gray. Count freshness shares the adjacent state
and age of its usage observation. On source or transport failure, cached facts
remain under STALE. Once the known earliest expiration passes, hide the indicator
until a newer authoritative poll establishes the remaining count and next expiry;
never guess how many expired or were used. A fresh poll after a reset used
elsewhere replaces both count and expiration together.

For installation, upgrade the API and firmware before enabling the new collector:
older strict v2 readers reject the extra field when present. New readers accept
both shapes, and existing private v3 state loads without changing its aggregates.
Before the collector's first new publication, stop collection and take an
owner-only backup of the private state directory and public data together. An
older collector cannot load private observations/archives containing the new
field; rollback must restore that matching pre-upgrade state/data backup with the
previous binaries. Keep the account scope key with the state backup. Never run old
and new collectors concurrently against the same state.
