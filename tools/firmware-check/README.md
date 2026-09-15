# Firmware validation

From the repository root:

```sh
python3 tools/firmware-check/check.py
# Use the ESPHome interpreter for this configuration check:
python3 tools/firmware-check/config.py
python3 scripts/firmware.py validate
python3 scripts/firmware.py build
.venv/bin/python tools/api-check/check.py
```

The native check needs a C++17 compiler with AddressSanitizer/UndefinedBehaviorSanitizer
and permission to bind temporary loopback sockets. It compiles the same parser,
state and HTTP transport used by the ESP32, checks contract fixtures and strict
mutations, then uses real sockets for framing and absolute-deadline scenarios.
Two timezone-localization fixtures are explicitly delegated to the API, which
owns the IANA database. Artifacts remain under ignored `artifacts/cm-006/native`.
The existing backend wrapper needs its documented Python jsonschema environment.

`device.py` serves synthetic data over the selected Mac LAN interface and records
only fixed serial diagnostic lines. It requires pyserial and a private settings
JSON containing `address`, `port`, and the firmware's dedicated `token`; never use
a provider token. Its output path must be under repository `artifacts/`.

```sh
# Use the Python selected by scripts/firmware.py (or another with pyserial).
python3 tools/firmware-check/device.py \
  --settings artifacts/cm-006/fixture-settings.json \
  --serial-port /dev/cu.wchusbserial10 --seconds 420 --reset
```

Close competing serial monitors. For bounded fault tests, an ignored package
overlay sets `meter_network.poll_interval: 10s` and `timeout: 2s`, preserving the
user's secrets file. The synthetic envelope's threshold is 30s. The
sequence alternates valid readings with malformed, oversized, chunked, truncated,
unsupported-version, 401 and stalled responses, freezes one observation past its
stale threshold, then stops/restarts the fixture listener. It records request
selection plus model/age/error/heap/uptime evidence, without bearer values, raw
response bodies or Wi-Fi settings. The process removes its listener on exit.

To exercise Wi-Fi interruption without changing the host network, an ignored
ESPHome package overlay may disable CYD Wi-Fi after 300s, re-enable it after 15s,
and log those two test markers. Compile that overlay with the same ESPHome
interpreter, then use `scripts/firmware.py flash` (the node name/build directory
must remain `codex-meter-1`). This test automation is absent from production YAML.
After testing, build the production YAML and flash it with the normal 60s/10s
settings from `secrets.yaml`.
A final smoke run uses `device.py --steady` for fresh samples with a 180s threshold.
The script's synthetic values are test data, not the user's live quota.

Verify the recorded device evidence with:

```sh
python3 tools/firmware-check/verify_device.py artifacts/cm-006/device/events.json
# After a final production capture lasting at least 135 seconds:
python3 tools/firmware-check/verify_device.py artifacts/cm-006/final-device/events.json --steady
```

The verifier requires each selected fixture's matching device result, retention,
monotonic age, explicit stale state despite repeated old HTTP success, API/Wi-Fi
recovery, continued heartbeats and no unexpected boot. `--steady` also checks
multiple normal 60-second polls. A server-only or zero-request run cannot pass.

Review `events.json` and serial output for accepted readings, exact failure codes,
retained observation/value, increasing age, recovery, uninterrupted heartbeat,
heap stability and absence of resets/panics. See the durable CM-006 report for the
actual run and its limits; a short fault run does not replace CM-010's 24-hour soak.
