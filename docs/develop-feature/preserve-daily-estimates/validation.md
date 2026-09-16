# Daily estimate preservation validation

## Result

Implemented and validated for the authorized local commit and collector deployment.
The public API contract and USB firmware remain compatible.

Both ponytail-review suggestions are applied: the three v3 fixture conversions
share one helper, and the backup condition uses `slices.ContainsFunc`.

- Quota corrections preserve daily totals and the published weekly gauge follows
  the source. A persistent high-water baseline prevents correction/rebound
  double-counting across restarts and interrupted publications.
- Reset jitter up to five minutes from the fixed anchor preserves overlapping
  calendar estimates, including midnight edge cases. Changed edges stay partial.
- Older ambiguity recovery keeps matching daily evidence. Material calendar
  replacement saves a bounded previous-state backup before committing.
- Private v4 migration retains v3 observations, calendar evidence and archives,
  saves an exact owner-only pre-upgrade backup, and blocks publication if that
  backup fails. Empty and ambiguous v3 records are covered too.

## Checks

- `tests/desktop/check.py`: passed formatting, race-enabled Go tests, vet, native
  builds, 51 v1 and 53 v2 fixture classifications, 17 contract tests, 15 desktop
  tests, 52 generated collector snapshots, 17 API responses and the retained-history
  integration check (six periods).
- `tests/firmware/check.py`: passed 53 usage fixtures, 8,204 decoder memory-safety
  cases, 14 malformed mutations, 19 missing-field mutations, 14 HTTP cases and
  cached-state checks. Host-only timezone validation retains its existing scope.
- `tests/display/check.py`: passed 41 fixtures, 312 render cases, sanitizer checks,
  all four orientations, paged drawing, portrait/landscape pixels and all 90
  landscape image baselines.
- `tests/firmware/config.py`: all 18 cases passed.
- `tests/display/check_touch.py`: calibration, orientation, persistence and
  tap/drag/debounce checks passed.
- An offline run of the built collector against an isolated copy of the current
  installed v3 state preserved the observation, bounds and daily entries and
  produced an exact pre-upgrade backup. It used a missing test credential path,
  made no provider requests and did not change the live installation.
- `git diff --check`: passed. The final source, tests, migration/backup handling
  and documentation diff were reviewed in-session.
- After the ponytail simplifications, the full desktop gate passed again. All
  eight publication-scanner tests and the source/history secret scan passed;
  `govulncheck ./...` reported no vulnerabilities.

The sandbox initially blocked test-only loopback listeners; the complete desktop
and native firmware suites passed with permission for those local test servers.
ESPHome's existing Python environment supplied the config/touch dependencies.

## Limits and installation

The chart estimates percentage points of weekly allowance, not raw token counts.
It can undercount after a downward allowance adjustment until usage exceeds the
saved peak. Existing outage and cross-midnight gaps remain partial/unknown.
The change prevents future loss; it does not reconstruct previously erased totals.

Install the validated collector with a coherent backup of the old binary, state
and published data, then restart its owned LaunchAgent. The public v2 API and
firmware are compatible. Older collectors cannot read private v4, so rollback
must restore their matching state and binary together.

The commit includes the collector source and tests, desktop snapshot inventory,
README/collector/contract/installation documentation and this plan/validation
directory. Runtime files and private validation artifacts are excluded. Deployment
records identify the committed source, installed binary, coherent backup and
fresh API/USB observations in ignored local artifacts.
