# Native usage snapshot collector

This Mac process polls the general Codex weekly quota every 60 seconds and
atomically publishes the [v2 contract](../../contracts/v2/README.md). It reads
the native credential file on each attempt and only calls the fixed HTTPS
usage GET. Codex continues to own sign-in and credential
renewal. The internal endpoint is a compatibility dependency; a response or
authentication change produces a fixed error code rather than fabricated quota.
Keychain-only authentication is not supported by this adapter.

## Build and run

From the repository root, with Go 1.26.8 or a newer patched release:

```sh
mkdir -p artifacts/bin
go build -trimpath -ldflags='-s -w' -o artifacts/bin/meter-collector ./cmd/meter-collector
artifacts/bin/meter-collector --once
artifacts/bin/meter-collector
```

The `--once` invocation polls once; exit 0 means a fresh snapshot, 1 means a source or
storage failure, and 2 means invalid configuration. The long-lived process
handles SIGINT/SIGTERM and releases its locks. It starts no child processes.
Use the [macOS installation guide](../../docs/installation.md) for automatic
startup, recovery and safe uninstall.

Configuration is explicit CLI option > inherited environment > selected literal
env file > defaults. Use `--env-file .env` to select a file; files are never
auto-sourced or executed. Values are literal: shell variables, quotes, command
substitution and inline comments are not evaluated. Unknown `METER_` settings
and duplicate file keys fail startup. Known API settings are accepted in a
shared env file but never used by the collector; an empty API-token
template therefore does not prevent collector-only operation.

| CLI option | Environment setting | Default |
| --- | --- | --- |
| `--auth-file` | `METER_AUTH_FILE` | `~/.codex/auth.json` |
| `--data-dir` | `METER_DATA_DIR` | `.local/data` |
| `--state-dir` | `METER_STATE_DIR` | `.local/state` |
| `--timezone` | `METER_TIMEZONE` | Discover Mac IANA zone once |
| `--collection-seconds` | `METER_COLLECTION_SECONDS` | 60 |
| `--stale-seconds` | `METER_STALE_SECONDS` | 180 |
| `--source-timeout-seconds` | `METER_SOURCE_TIMEOUT_SECONDS` | 10 |

Paths resolve against the selected env file's directory, or the working
directory with no file. Only leading `~/` expands. Existing symlinks are
resolved before checking that data, private state and the credential directory
are disjoint. Dangling symlinks and overlapping paths fail safely; missing data
and state directories are created. Empty required
values are invalid; empty timezone explicitly requests discovery. If discovery
fails, supply a valid IANA zone such as `--timezone America/Los_Angeles`.
Collection allows 10–300 seconds, timeout 1–30 seconds and no longer than
collection, and stale age 30–3600 seconds and at least twice collection.

## Files, failure and recovery

`usage.json` holds only the current reset period and remains capped at 4,096
bytes. `history.json` exposes retained daily aggregates with a separate 64 KiB
cap. Neither contains raw account identifiers, credentials, or unrelated account
fields. Private state v4 combines the active calendar ledger, reset anchor,
observation and high-water baselines, and retained period snapshots in `observation.json`
(64 KiB cap), alongside the existing 32-byte `installation.key`.

Periods intersecting the latest 35 days are retained whole. The account scope
is HMAC-SHA256 under the installation key; an account/key change starts new
history. Upgrades save the original private file once as `observation.pre-v4.json`.
Version 3 migration preserves all existing daily evidence and initializes the
high-water baseline from the last observation. Version 1/2 migration preserves
the authoritative observation and ambiguity/reset anchor, starts calendar days
unknown, and fences the first attribution interval. A failed backup prevents
publication. Corrupt state starts unknown; an insecure existing key fails
startup. Replacing an established calendar ledger also saves one bounded
`observation.previous.json` backup. Older collectors cannot read v4; stop collection and restore the matching
backup/binaries before rollback. See the [installation guide](../../docs/installation.md).

State must be owner-only (0700 directory, 0600 files). Data is created with 0750
directory permissions (subject to caller umask) and 0640 snapshots.
Pre-existing directories with more permissive modes are rejected. Configure
the later container's group access to the data directory; mount only that
directory read-only. Provider credentials are opened read-only and never chmodded.

An exclusive advisory lock in each data/state directory rejects duplicate
publishers, including different state directories aimed at the same data path.
Locks are released by the OS on process exit, including crashes; the small lock
files remain in place to preserve inode identity. One fixed temporary filename
per output is reused after crashes. Each write is flushed, fsynced and renamed
on the same filesystem, followed by directory fsync. Temporary symlinks are
refused. A failure before public rename leaves the complete previous snapshot.
Private state is written first, so restart can recover a newer observation
after a crash between the private and public writes. A publication failure also reloads that
committed private record before another poll, preserving its ledger and baseline.
Retained aggregates are bounded; no raw source logs are rescanned. Restarting the read-only container
cannot change the native ledger.

HTTP redirects, proxies and compressed responses are disabled. Authentication
files and response bodies have 64 KiB and 256 KiB caps. The adapter selects
the general Codex bucket at top-level `rate_limit` and exactly one window
with a duration of 604,800 seconds.
Alternate-model buckets are ignored. Missing, negative or non-finite quota
fails collection; finite over-quota values clamp to zero remaining. An invalid
optional earned-reset count becomes null.

Source observation timestamps supplied as root `observed_at` are preserved
and validated. Otherwise the timestamp is response-body completion. Equal observations never become
newer merely by being replayed; older or conflicting same-time observations
produce `source_invalid` while retaining the previous reading. Account changes
replace the baseline and scope. Equal-time conflicts break interval coverage
without clearing daily estimates; an older sample is ignored for accounting.

## Daily consumption and reset handling

The chart partitions the fixed 168-hour reset period at local midnight, yielding
seven to nine calendar-date intervals. The first and last dates are clipped to
the exact reset period. A Saturday 5 a.m. reset includes both Saturdays, even when
today advances to Sunday. The current date never shifts or discards columns.
Timezone changes repartition the active period and preserve only totals whose
intervals exactly match; completed periods retain their original timezone.
Elapsed intervals start unknown; future intervals show zero placeholders.
Installing mid-cycle never assigns pre-install usage to a day.

Compatible readings no more than twice the configured poll interval apart add
positive used-percentage changes to a single slot. For example, 20% then 35%
records 15 percentage points once. Equal readings establish measured zero;
duplicate observations add nothing. An interval ending exactly at a slot boundary
belongs to the closing slot. Positive changes crossing a boundary are discarded,
while zero changes can cover both sides. Failed/invalid readings and longer gaps
break interval coverage. Known portions survive as partial amounts; a closed
slot is complete only when attributable intervals cover its entire duration.

Reset timestamps within five minutes of the fixed accounting anchor are compatible;
the source timestamp is still published exactly. Overlapping intervals on the same
calendar date retain their estimates, with changed edge intervals marked partial.
The tolerance never follows a moving previous timestamp.

A quota decrease preserves earlier daily totals. The collector remembers the
highest observed usage in the accounting period and adds only increases above it.
For example, 30% → 29% → 30% adds nothing; a subsequent 31% adds one point.
This prevents correction/rebound double-counting, but may undercount usage after
an allowance adjustment. The weekly gauge always follows the current source.

A larger reset-time change makes history ambiguous, including
early resets performed elsewhere in Codex. The fresh gauge and source reset remain
visible, but elapsed chart values become unknown. Two timely observations with
the same reset and nondecreasing usage establish a new observed baseline. The
interval into that baseline is discarded, and earlier totals for matching calendar
intervals survive. The next compatible interval resumes partial daily accounting.
Previously ambiguous installations recover the same way.
This does not confirm an early reset or reconstruct missing daily totals. Only
completed periods and one current snapshot are retained as the reset changes.
The adapter exposes no trusted early-reset event or allowance-definition
metadata, so a count decrease alone cannot confirm a reset and hidden allowance
changes cannot be distinguished. An increment that would push the total above 100
points is skipped while preserving earlier estimates. This is approximate quota movement, not token or
billing accounting. See the [normative interval/reset rules](../../contracts/v2/README.md).

Only a new source observation at or after the current accounting reset, with a
compatible later reset, confirms a scheduled cycle transition. Missing entire
cycles creates no intervening history. Time passage alone leaves the old cycle
and last-known allowance stale.

A failed poll retains the last observation and publishes stale status with a
fixed source error. Recovery clears that error on the next successful reading.
Retries wait 60, 120, 240, then at most 300 seconds by default, without overlap;
success restores the normal interval. Logs report only state transitions, with
no quota values, credentials, URLs, paths or upstream error text. The process
creates no log files. The launchd installation sends native output to `/dev/null` to bound logs.

Publication timestamps never move backwards. During a clock rollback the
collector keeps the previous complete file and logs `clock_error` until the
clock catches up; the API's contract projection can flag the retained future
timestamp. Passing a reset time without a new observation never renews quota.

Run the [desktop checks](../../tests/README.md#desktop-service) before
staging collector changes. Validate real device behavior separately after firmware changes.

Optional reset-credit details add at most one read-only request per successful
positive-count poll, within a two-second sub-deadline. The normalized snapshot
includes the earliest expiration only when the detail list covers every available
credit. Detail failures preserve weekly quota and count with unknown expiration.
See [v2 reset semantics](../../contracts/v2/README.md#optional-earned-reset-indicator-cm-009).
