# CYD network client

The network client supplies a cached usage model and a gauge/chart.
The LCD uses 8-bit color and a quarter-frame buffer.
A fixed-size view stores the percentage, age,
reset text, coverage and source-ordered weekday labels. The display checks for
visible changes once a second and redraws only when needed. One frozen view is
replayed across all four buffer pages, preventing mixed-frame text or bars.

Tap and release **CODEX** to turn the view a quarter turn. Starting in the original
landscape position, the bottom moves to the original right, top, left, then bottom
edge. Portrait uses a 240×320 layout with the reset offset beneath the date; the
original 320×240 landscape layout is unchanged. Hold does not repeat, and dragging
outside the title cancels the tap. The touch target includes padding around the
title. Contacts shorter than 40 ms and new contacts within 180 ms of release are
ignored to reduce noise and bounce.

The ESP32 stores the selected position in its NVS flash using a dedicated one-byte
preference. Each accepted change saves and syncs once; drawing and polling do not
write it. A missing or invalid preference starts in the original landscape view.
A save failure leaves rotation usable and emits a `meter_rotation` warning. Normal
firmware writes preserve this preference; erasing flash removes it. Rotation keeps
the same 19,200-byte display buffer and takes effect between complete paged frames.

Touch uses the XPT2046 on its own SPI bus (CLK25, MOSI32, MISO39, CS33, IRQ36).
The `touch_calibration` limits in `codex-meter-1.yaml` began with an example.
Physical corner readings verified that raw X runs down and raw Y runs right, so
`touch_transform.swap_xy` must be true for this CYD. `touch_transform` describes the original
landscape view, independent of the selected position. Keep the touchscreen's own
transform unset: the component maps raw coordinates itself because ESPHome caches
logical touch dimensions at setup. For calibration, capture `meter_rotation` press
lines while touching known corners in the original landscape view, determine any
axis swap/mirroring and calibrated raw limits, then verify the title and nearby
non-title points in all four positions. Keep physical validation records under
ignored `artifacts/` and use the [display checks](../tests/README.md#firmware-and-display).

The quota number is rounded to the nearest whole percent; positive values below
1% show `<1%`, and values above 99% but below 100% show `>99%`. The gauge follows
the underlying value. Reset text retains the full local date, 12-hour time and
explicit UTC offset. `RESET DUE - LAST KNOWN` preserves the old quota/history
until a valid new observation arrives. `FRESH`, `STALE`, `WAIT` and a compact
age or offline marker make status visible independently of color.

Daily bars use a fixed 0–100 percentage-point scale of weekly allowance. Complete
values are solid; partial values use `~` and stripes. Measured zero is `0`,
partial zero `~0`, unknown history `?`, and future zero `0>` with zero bar height.
Positive values below one point show `<1` (or `~<1`) with a one-pixel bar. The footer
explains coverage symbols. Supplied labels, including repeated weekdays across
DST, retain their slot order. Available reset credits appear on the right side
below the freshness state.

`meter_display` serial lines record the drawn view and total LCD update duration,
including all buffer passes and SPI. They contain personal usage data and must
stay private. The
ten-second heartbeat remains. Native checks and optional fixture images are documented in
[tests/README.md](../tests/README.md#firmware-and-display).

Copy `secrets.example.yaml` to ignored `secrets.yaml`, set Wi-Fi and a dedicated
64-character lowercase hexadecimal API token, and set
`http://<Mac LAN IPv4 address>:<port>/v2/usage`. The API must explicitly bind that
LAN address and use the same token. Neither provider credentials nor account
controls belong in firmware. Build outputs contain private settings and stay
ignored. The client accepts a numeric IPv4 host and port 1024–65535; it does not
perform DNS, redirects, TLS, discovery, or public service setup. Plain HTTP sends
the meter token and usage data unencrypted on the configured trusted LAN.

Default polling is 60 seconds, with a 10-second total request deadline. Whole
second overrides allow polls 10–300 and timeouts 1–30, no longer than the poll.
A response's stale threshold must be at least twice the configured poll interval.
Failed requests retry after the poll interval, then double up to 300 seconds;
a valid response restores normal cadence. Wi-Fi reconnects through ESPHome with
`reboot_timeout: 0s`; no disconnection-triggered reset is configured.

A single persistent FreeRTOS worker owns a nonblocking socket. Connect, send,
headers, body and chunk framing share one monotonic deadline. Headers and trailers
have separate bounds; decoded bodies are capped at 4096 bytes plus a trailing NUL.
Content-Length and chunked responses are supported. Ambiguous framing, compressed
bodies, non-200 status, unsupported content types and truncated data fail closed.
Sockets close after each attempt. The main loop consumes one fixed-size result
queue; it alone changes the cached model and writes sanitized diagnostics.
No HTTP operation blocks LCD redraw or age progression.

The allocation-free decoder validates the exact v2 shape, keys (including escaped
duplicates), JSON number grammar, lengths, types, ranges, timestamp/freshness
arithmetic, cycle/slot relationships, and reset text's date/UTC-offset arithmetic.
As specified by the contract, the Mac owns IANA-zone conversion and the weekday
labels. The CYD bounds timezone syntax and label enums but does not carry an IANA
database: the API's authoritative validator rejects nonexistent zones, incorrect
weekday labels, and incorrect calendar boundaries. Native tests identify the
three fixture cases delegated to the host validator.

`state()` exposes the last accepted model. Display consumers must use
`state.status(monotonic_ms())`, `state.age(...)`, and `state.day(index, ...)` to
show live age/freshness and project expired future slots. These accessors do not
fabricate a reset. Repeated observations cannot decrease age, including its
subsecond remainder. Older observations/publications and equal-time conflicting
facts are refused; only legitimate API projection may change equal-time history.
A changed non-null scope replaces the old account's data. An unavailable response
after a previous valid observation preserves the last-known reading as failed;
first-boot unavailable data has placeholders. Transport failure clears only after
a valid, accepted response.

Run `python3 scripts/firmware.py validate`, `build`, and the documented USB `flash`
command from the repository. ESPHome Builder mirrors also need their own matching
private secrets. Native checks are documented in
[tests/README.md](../tests/README.md#firmware-and-display).

The display includes a compact reload icon plus available-reset count below the freshness state
on the right, top-aligned with the main percentage, in all four orientations.
Zero/unknown count leaves no indicator.
The earliest available expiration controls blue (>7 days), yellow (4–7 days), or
red (<4 days); unknown expiration is gray. The existing state/age applies to the
count. A passed expiration hides the indicator until the next source observation.
This icon has no reset action. No extra framebuffer or font asset is used.
