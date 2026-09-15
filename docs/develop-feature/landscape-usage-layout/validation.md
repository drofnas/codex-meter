# Landscape layout validation

Implemented on `feature/landscape-usage-layout`, based on updated `main`
(`c2104bc`), following the [plan](plan.md).

## Result

Both landscape positions place the reset counter in the header and omit the
status, age, scale callout, and footer legend. `DAILY USAGE %` uses solid bars
with rounded plain numbers, `?` for unknown readings, and `0` for future days.
Today's value, bar, and weekday are yellow. The previous day returns to its
normal colors as the cached clock advances, including during disconnection.

The updated README image is rendered from the existing synthetic eight-day
example, also used by the portrait image. It shows 64% remaining, two resets,
and Tuesday highlighted. No live account data appears in the screenshot.

## Checks

| Check | Result |
| --- | --- |
| `tests/display/check.py --output artifacts/check/landscape-layout` | Passed: 41 oracle-validated fixtures, 312 renders in four orientations, sanitizer bounds, paged-frame equality, and landscape/portrait pixel checks. |
| Portrait comparison against `main` | All 156 portrait PPM renders and the README portrait PNG are byte-identical. |
| Landscape baselines | Intentionally regenerated all 90 hashes after independent pixel checks; the full display suite matched them. |
| `tests/firmware/check.py` | Passed: 53 usage fixtures, 14 malformed and 19 missing-field inputs, 8,204 decoder memory-safety cases, cached state, and 14 loopback HTTP cases. |
| `tests/firmware/config.py` | Passed: 18 configuration cases. |
| `tests/display/check_touch.py` | Passed: captured calibration samples, four orientations, persistence, title tap, drag, and debounce checks. |
| `tests/desktop/check.py` | Passed: Go formatting, race tests, vet/builds, both contract versions, 32 Python tests, 51 generated snapshots, 17 oracle responses, and the history API check. |
| `python -m unittest discover -s tests/security` | Passed: all eight publication tests, including rejection of unrelated or appended secrets in the checksum file. |
| `govulncheck ./...` | Passed: no vulnerabilities found. |
| `scripts/firmware.py validate` | Passed with the installed ESPHome interpreter. |
| `scripts/firmware.py build` | Passed: image 745,095 bytes; static DRAM 47,292 bytes; IRAM 79,211 bytes. |

The frozen frame remains 252 bytes, with the existing 19,200-byte paged display
buffer. Configuration, build, and device logs stay under ignored
`artifacts/private/landscape-layout/`. The native test reports and fixture
renders stay under ignored `artifacts/check/`.

## Visual and transition coverage

Inspected synthetic PNGs for the README example, nine-day DST cycle, unknown
history, and maximum reset count. Pixel checks cover solid fills, zero-height
bars, current/non-current value and weekday colors, reset-counter placement,
and the absence of age/status/legend/scale text. Existing offline boundary
scenarios verify the highlight before, at, and after midnight, repeated weekday
labels, and an expired cycle with no current interval.

Status and partial-coverage fields cannot alter either layout's pixels. Stale
weekly colors and reset-count expiration colors retain their existing behavior.
The exact checksum allowlist includes the revised synthetic auth-failure images
and retains historical entries; it does not allow arbitrary hash values.

## USB confirmation

After the implementation commit, deploy with `scripts/firmware.py flash` to the
connected USB port, separately verify the generated flash regions, and capture
a successful boot and display updates. These implementation results precede
device deployment. Physical LCD appearance and touch operation require viewing
the device; render tests and serial logs cannot substitute for that confirmation.
