# CYD display validation

Run from the repository root, using the existing contract environment for the
oracle and the ESPHome interpreter for Pillow/device operations:

```sh
.venv/bin/python tools/display-check/check.py
python3 tools/display-check/preview.py
python3 scripts/firmware.py validate
python3 scripts/firmware.py build
```

`check.py` compiles the exact portable device renderer with ASan/UBSan. It validates
synthetic envelopes with the independent contract oracle, checks every drawing
rectangle against the 320×240 or 240×320 bounds in all four orientations, verifies
quota/reset/label/coverage semantics and equality of full versus paged rendering,
and produces native PPMs, a manifest and timing measurements under ignored
`artifacts/cm-007/native/`. `preview.py` requires Pillow and creates PNGs plus a
landscape, portrait and orientation fixture sheets. Orientation previews show each
view upright for reading; the physical rotation order needs device confirmation.
Native pixels approximate the device's 8-bit RGB quantization;
physical color and readability still need confirmation on the LCD.

Coverage includes full/exhausted/missing data, measured/partial/future zero,
sub-percent consumption, unknown past, stale/offline/reset expiry, both DST
transitions (seven to nine calendar dates), 12-hour midnight/noon formatting,
whole-hour and fractional offsets,
and dates near the upper supported epoch. Source labels are used in array order.
Each measurement draws 100 frames without SPI on the host. Device logs separately
measure the whole LCD update including all four buffer passes and SPI transfers.

The same wrapper compiles and runs `orientation.cpp` with ASan/UBSan. It checks all
307,200 panel-pixel mappings across the four positions, requested bottom-edge
order, title hit testing, tap/hold/drag/debounce behavior, and preference restore,
cycling and failure paths. These tests do not verify the physical touch wiring or
calibration.

Run the physical-axis regression with the ESPHome Python (PyYAML is included):

```sh
.venv/bin/python tools/display-check/check_touch.py
```

This reads the production YAML's public calibration/transform values without
resolving or printing secrets, then runs the sanitized orientation test with those
exact values. Three real CYD corner samples are checked in all four orientations,
plus four measured CODEX presses checked against the exact shared title rectangle.
The original unswapped configuration fails this test. The readings establish axis
direction with a 24-pixel allowance for taps near corners; title accuracy across
the LCD still requires an actual title-tap check after installation.

Use a separate output directory to preserve an existing acceptance capture:

```sh
.venv/bin/python tools/display-check/check.py \
  --output artifacts/title-rotation/native \
  --landscape-baseline artifacts/title-rotation/baseline/landscape-hashes.json
python3 tools/display-check/preview.py --output artifacts/title-rotation/native
```

The optional baseline is a JSON object mapping original scenario names to SHA-256
hashes of PPMs rendered before the change. With it, the matching landscape
scenarios must remain pixel-identical. A v1 baseline is intentionally incompatible
with the new reset formatting and calendar-day layouts. Omit it when the historical artifact is
unavailable; the semantic, bounds, paging and input checks still run. Use a Python
interpreter with Pillow for previews. Build and validate the firmware separately,
then record the physical checks under ignored `artifacts/`. Never start a device
sequence while another capture owns the serial port.

For a bounded physical sequence:

```sh
.venv/bin/python tools/display-check/prepare_device.py
# Build an ignored package overlay including codex-meter-1.yaml, with only
# meter_network.poll_interval: 10s and timeout: 2s; retain the same node name.
python3 scripts/firmware.py flash --port /dev/cu.wchusbserial10
python3 tools/firmware-check/device.py \
  --settings artifacts/cm-006/fixture-settings.json \
  --serial-port /dev/cu.wchusbserial10 --seconds 360 --reset \
  --display-plan artifacts/cm-007/display-plan.json \
  --output artifacts/cm-007/device
python3 tools/display-check/verify_device.py artifacts/cm-007/device/events.json
```

Use the ESPHome Python for these device commands. The private settings file uses
the existing dedicated meter token/address/port, never a provider token. The
fixture plan stays under artifacts and uses distinct synthetic scopes so historic
DST fixtures can follow current fixtures without defeating source ordering.
Each screen lasts two 10-second polls. Reset expiry is followed by a 16-second
API outage and recovery. The final normal fixture remains until the bounded
capture ends; no fixture listener survives process completion.

Record user/visual confirmation of layout, labels, colors and legibility for the
physical sequence. Serial evidence alone cannot satisfy this gate. Then rebuild
and flash the production YAML (normal 60s/10s settings), and capture at least 135s
with `device.py --steady`. Verify its normal cadence with
`tools/firmware-check/verify_device.py .../events.json --steady` and inspect its
`meter_display` lines. No temporary polling override or test sequence belongs in
the final firmware. Run the native firmware and API regression wrappers before
final staging. CM-010 owns the longer soak.

CM-009 fixtures cover hidden zero/unknown counts, neutral unknown expiration,
blue/yellow/red day boundaries, expiry while disconnected, stale availability,
maximum count layout, and replacement after simulated redemption elsewhere.
The preview helper also emits `reset-sheet.png` and `reset-portrait-sheet.png`.
