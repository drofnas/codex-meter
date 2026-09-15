# Portrait usage layout

Status: approved and implemented, 2026-09-15. Local validation passed; see
[validation results](validation.md) for the implementation checks and device
validation scope.

Branch: `feature/portrait-usage-layout`, created from `main` at
`d62e74cfed5111f1b12d04dd953e83b722975ba8`.

## Goal and scope

Apply the user's written changes and sketch to both 240×320 portrait positions
(1 and 3). Preserve both 320×240 landscape positions (0 and 2), including their
rendered pixels, labels, status, coverage markers, and reset-counter placement.
This is a display update; collection, API contracts, usage accounting, polling,
rotation, touch targets, persistence, and reset-credit behavior retain their
current semantics.

## Approved portrait layout

All coordinates are logical display pixels, before rotation. Use the existing
bitmap font, dark background, and weekly blue/cyan accent (`GOOD`).

| Element | Proposed placement and behavior |
| --- | --- |
| Header | `CODEX` remains at (12, 10). Remove the status dot and the entire `FRESH`/`STALE`/`WAIT` value. Put the reload icon and reset count at the top right, ending at x=228. |
| Weekly value | Keep the large percentage at (12, 42), font scale 6. |
| Weekly label | Put `WEEKLY` and `REMAINING` on two lines at x=156, y=49/64, font scale 1. This fits beside `100%` and `>99%` as well as shorter values. |
| Weekly progress | Full-width track from x=12 to x=228 at y=92, height 5. |
| Reset details | `RESETS AT` at (12, 106); full date, 12-hour time, and offset together at (12, 119), font scale 1. Remove the age/offline text. |
| Daily heading | A subtle divider at y=135, then `DAILY USAGE %` at (12, 145), font scale 1. |
| Daily rows | Three-letter weekday at x=12, font scale 2. One solid horizontal progress track from x=60 to x=228, height 14, with its value centered inside. First row y=162. |
| Row spacing | Use a 20-pixel pitch for 7–8 rows and an 18-pixel pitch for 9 rows. The final row always ends by y=320. No scrolling. |
| Footer | Remove the coverage legend. |

The font only supports whole-number scales. Scale 1 is 7 pixels high; the current
date/time uses scale 2 (14 pixels). A long timestamp such as
`2026-09-19 12:11 AM (+5:45)` is 161 pixels wide at scale 1, within the 216-pixel
content width. The largest existing weekly strings are 138 pixels wide at scale
6; `REMAINING` is 53 pixels wide at scale 1. Validate readability on the actual
LCD during implementation.

## Daily values and colors

- Keep source interval order and display every supplied day: 7–9 rows, including
  both partial reset dates and repeated weekdays. Expand labels for portrait
  only: `M/T/W/Th/F/Sa/Su` → `MON/TUE/WED/THU/FRI/SAT/SUN`.
- Each bar remains a fixed 0–100% scale of the weekly allowance consumed on that
  date. Do not normalize to the largest day or prorate partial dates.
- Known complete or partial values display the nearest whole percentage with a
  `%` suffix. Measured zero and future zero both display `0%`. Unknown values
  display `?` with an empty track.
- Remove `~`, `<`, and `>` from portrait **daily** values, and use solid fills
  without partial-coverage stripes. For example, `~20` becomes `20%`, `0>` becomes
  `0%`, and a known 0.2% becomes `0%`. Retain raw-value precision for bar width,
  with one visible pixel for positive values that would otherwise round to zero.
  The existing weekly `<1%`/`>99%` formatting is unaffected.
- Highlight today's weekday and filled bar with the existing yellow
  (`RESET_YELLOW`), regardless of coverage, value, or connection status. All
  other weekday labels and filled bars use the weekly blue/cyan (`GOOD`), even
  for a previous day's partial 20% value. Empty tracks remain neutral.
- Center values inside tracks. Keep their glyphs legible on both the neutral
  track and the bright fill by changing text ink where it crosses the fill.
  The weekday and filled segment identify today's row even when its value is
  unknown or zero.
- Find today's row using `day.start <= state.as_of(now) < day.end`, with the
  source's already computed local-calendar intervals. Do not match weekday
  strings or apply the reset timestamp's UTC offset to other dates; either can
  select the wrong row across repeated weekdays or daylight saving changes.
- Include the selected row in frame equality so its highlight advances at a
  day boundary, including while offline. Once the cached cycle ends, highlight
  no row until a newly confirmed cycle contains the projected current time.
  Preserve the existing `RESET DUE - LAST KNOWN` behavior and placeholders.

## Other behavior retained

Moving the reset counter changes only its portrait coordinates. Keep exact
counts, automatic sizing for long counts, hiding for zero/unknown/expired counts,
and existing expiration-based colors. The indicator remains display-only.

Removing status and age hides those portrait elements; the cached model still
tracks freshness and failures. Preserve the existing weekly warning color when
stale and the reset-due message. Daily colors depend only on today's interval.

## Implementation steps

1. Capture synthetic reference renders from the unchanged renderer for both
   landscape positions using the existing display runner. Retain their hashes
   under ignored `artifacts/` for comparison after changes.
2. In `firmware/components/meter_network/ui.h`, introduce a dedicated portrait
   drawing path selected by positions 1/3. Keep the landscape drawing operations
   and their shared formatted values intact.
3. Extend the fixed-size projected frame only with the numeric daily information
   and current-row index needed by portrait. Compute horizontal widths from
   `Day.delta`, not the existing 38-pixel vertical bar height. A compact option
   is a byte each for rounded daily percent and width per row, plus one current
   row index. Verify `sizeof(Frame) <= 256`; retain the 19,200-byte framebuffer,
   allocation-free rendering, and frozen-frame replay across all four pages.
4. Implement the layout and row styling above. Extend `ui::equal` for any added
   fields. Preserve `State::day` projection: a future zero becomes unknown when
   its interval starts without an observation.
5. Extend `tests/display/native.cpp` and `tests/display/check.py` with portrait
   assertions for values, fill widths, today selection, text fit, colors, and
   redraw boundaries. Test the new fields and actual rendered results while
   retaining existing landscape semantic expectations.
6. Refresh only `docs/images/portrait.png` with synthetic data. Update `README.md`
   and `firmware/README.md` to explain the two layouts. Clarify the orientation
   wording in `contracts/v2/README.md` where it describes the old counter position
   and visible status; do not change protocol semantics or schema.

## Acceptance and validation

- Both portrait positions show the requested header, side label, one-line reset
  timestamp, list of bars, simplified daily values, and today color, with no
  status indicator, age, or legend.
- Render 7-, 8-, and 9-row cycles, repeated Saturdays, daylight-saving changes,
  `0%`, `100%`, fractions, unknown and partial values, and first-boot placeholders.
- Check midnight transitions immediately before/at/after an interval boundary;
  only the current interval is yellow. Include a transition while offline and
  reset expiry while awaiting a new observation.
- Check long dates, two-digit 12-hour times, fractional/positive/negative UTC
  offsets, `100%`/`>99%` weekly values, maximum reset count, and counter expiration.
- Verify numeric text remains readable when a bar ends inside the centered value.
- Compare every landscape reference image byte-for-byte after the change;
  preserve `docs/images/landscape.png` exactly.
- Preserve drawing bounds, all four orientations, title-tap behavior, stored
  rotation, paged rendering equality, and the frame-size limit.

Implementation validation commands, from the repository root with the documented
development Python environment:

```sh
.venv/bin/python tests/display/check.py --output artifacts/check/portrait-usage-layout
.venv/bin/python tests/display/check_touch.py
.venv/bin/python tests/firmware/check.py
.venv/bin/python tests/firmware/config.py
python3 scripts/firmware.py validate
python3 scripts/firmware.py build
git diff --check
```

The local `.venv` was absent during planning; resolve an installed compatible
environment or create the documented development environment before validation.
Run the broader contribution/CI gates if this change is later prepared for
publication. A hardware flash and LCD readability/rotation check are subsequent
implementation validation, not part of this planning task.

## Planning evidence

Inspected the shared renderer, cached state and calendar interval rules, existing
portrait image, display fixtures and native runner, orientation code, and
contribution/validation instructions. Verified text widths against the existing
5×7 font formula. The initial planning phase created the branch and this plan;
firmware checks were subsequently run during the approved implementation.

The accompanying conversation mockup uses synthetic values and the existing
bitmap glyphs. A local JavaScript canvas stub checked 54 combinations of row
count, highlighted row, unknown value, and weekly percentage for runtime errors
and drawing bounds. Browser inspection of the local preview was blocked by the
browser URL policy; this check is not browser or hardware visual validation.
