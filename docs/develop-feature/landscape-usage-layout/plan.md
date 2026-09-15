# Landscape usage layout

## Scope

Update both 320×240 landscape positions from `main` (`c2104bc`) on
`feature/landscape-usage-layout`. The user authorized planning, implementation,
validation, a local commit, and USB deployment, and requested an updated README
screenshot.

## Changes

- Remove the header status indicator and age callout. Put the existing reset
  icon/count in the top-right header, with its existing expiration colors.
- Keep the weekly quota, gauge, reset timestamp, and vertical chart layout.
- Label the lower section `DAILY USAGE %`; remove the scale callout and footer
  legend.
- Display known daily usage as a rounded whole number without a percent sign
  or coverage prefix. Future days show `0`; unknown history shows `?`. Unknown
  current-day readings also remain `?` until observations arrive. Positive
  fractions retain their proportional minimum one-pixel fill.
- Draw solid daily bars. Highlight only the current interval's value, fill,
  and weekday in yellow. Other known values/fills use the original cyan;
  other weekdays keep their original white/muted colors. Reuse the existing
  interval-based current-day calculation so offline midnight, repeated weekday
  labels, DST boundaries, and expired cycles behave consistently with portrait.
- Generate `docs/images/landscape.png` from synthetic data through the native
  firmware renderer, and update README descriptions and image alt text.

## Validation and delivery

1. Capture all existing display renders before source edits for a pixel-level
   portrait comparison.
2. Extend native/pixel checks for landscape values, header placement, absent
   status/age/legend, solid fills, and current-day color transitions. Intentionally
   regenerate landscape checksums after these checks and visual inspection.
3. Run display, native firmware, configuration, touch, and repository checks;
   validate and build the ESPHome firmware. Review the exact final diff and
   publication scan before committing only the scoped changes.
4. Flash the connected USB device with the documented helper, verify all written
   regions, and capture a successful boot and display update in private logs.
   Physical LCD confirmation comes from viewing the device; synthetic renders
   and serial logs alone cannot confirm its appearance.

No changes to the collector/API contract, portrait pixels, credentials, display
buffer, touchscreen calibration, rotation persistence, or reset behavior are
needed. Build images and live device logs remain in ignored directories.
