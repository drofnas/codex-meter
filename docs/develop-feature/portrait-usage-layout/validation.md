# Portrait layout validation

Implemented on `feature/portrait-usage-layout` after approval of the
[layout plan](plan.md).

## Result

Both portrait positions use the revised header, side label, one-line reset
timestamp, and daily horizontal progress list. Only today's weekday and filled
daily bar are yellow; other daily rows stay blue/cyan. Daily values are whole
percentages or `?`, without coverage markers, stripes, status, age, or a legend.
The native-rendered preview is in `docs/images/portrait.png`.

Both landscape positions match their original pixels. The implementation adds
20 bytes to the frozen frame (232 → 252 bytes), within the 256-byte limit enforced
at compile time and by tests. The 19,200-byte paged display buffer is unchanged.

## Checks run

| Check | Result |
| --- | --- |
| `tests/display/check.py --output artifacts/check/portrait-layout` | Passed: 41 synthetic fixtures, 312 renders across four orientations, sanitizer bounds checks, paged-frame equality, and portrait pixel checks. |
| Landscape comparison | All 90 original landscape PPM hashes matched. The existing `docs/images/landscape.png` also matched Git byte-for-byte. |
| `tests/firmware/check.py` | Passed: 53 usage fixtures, 14 malformed inputs, 19 missing-field inputs, 8,204 decoder memory-safety cases, cached-state checks, and 14 loopback HTTP scenarios. |
| `tests/firmware/config.py` | Passed: 18 configuration cases using the installed ESPHome interpreter. |
| `tests/display/check_touch.py` | Passed: captured calibration samples, title taps, all four rotations, persistence, and tap/drag/debounce behavior using the installed ESPHome interpreter. |
| `python3 scripts/firmware.py validate` | Passed. |
| `python3 scripts/firmware.py build` | Passed: ESP32 program compiled; image size 745,451 bytes, static DRAM 47,292 bytes, IRAM 79,211 bytes. |
| `git diff --check` | Passed. |

Native display and firmware tests ran with the repository's pinned development
dependencies in `.venv`. The loopback HTTP tests and ESPHome build needed
approved access beyond the filesystem/network sandbox; both passed on retry.
Build and configuration logs remain under ignored `artifacts/private/`.

## Coverage and visual review

- Seven, eight, and nine calendar intervals, including repeated weekdays and
  daylight-saving boundaries.
- Current-day selection before, at, and after local midnight, including offline
  transitions and an expired cycle awaiting confirmation.
- Whole-percentage rounding, positive sub-percent fills, zero, unknown, full
  bars, and partial historical values that retain blue/cyan.
- A redraw when horizontal bar width changes without changing the old vertical
  height/label, and when only the current-day highlight changes at midnight.
- Pixel checks for solid fills, current/non-current weekday colors, contrast
  across centered values, hidden status/age/coverage text, and absence of a footer.
- Inspected native-rendered PNGs for the eight-row example, nine-row DST cycle,
  maximum reset count, and a half-filled bar whose value crosses the fill edge.
- Existing fixtures exercise full/near-full weekly values, long dates, UTC
  offsets, reset-count expiration, stale data, and first-boot placeholders.

## Device validation scope

Implementation validation above preceded USB deployment. After installation,
verify flash contents and successful boot using the documented firmware tools;
keep device logs under ignored `artifacts/private/`. On-device readability of
the smaller bitmap text, physical rotation/touch, and stale/recovery behavior
require a hardware check. Native images and tests do not measure the actual LCD
update time or viewing conditions.

These results describe the implementation checks, independently of later commits
or deployment records.
