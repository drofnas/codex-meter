# Tests

The running application uses `cmd/`, `internal/`, `scripts/`, and `firmware/`.
This directory contains development checks. Go unit tests stay next to their
packages; collector source fixtures live in `internal/collector/testdata/`.
The JSON files in `contracts/*/fixtures/` are synthetic test inputs, not captured
account data or previous test output.

## Desktop service

From the repository root, install `tests/requirements.txt` in a Python environment
and run:

```sh
.venv/bin/python tests/desktop/check.py
```

This runs Go formatting, race tests, `go vet`, collector/API builds, Python
configuration and lifecycle unit tests, both protocol fixture suites, an
independent check of generated snapshots/responses, and a temporary loopback
history API. It uses synthetic data and does not start installed services,
Docker, or provider requests.

For focused unit tests:

```sh
go test ./...
.venv/bin/python -m unittest discover -s tests/desktop
.venv/bin/python -m unittest discover -s tests/contracts
```

## Firmware and display

Use a C++17 compiler and the development environment from
[CONTRIBUTING.md](../CONTRIBUTING.md):

```sh
.venv/bin/python tests/firmware/check.py
.venv/bin/python tests/firmware/config.py
.venv/bin/python tests/display/check.py
.venv/bin/python tests/display/check_touch.py
```

These check parsing, HTTP framing/deadlines, cached state, drawing bounds, all
four orientations, touch calibration, and preference persistence. Native checks
use AddressSanitizer and UndefinedBehaviorSanitizer; no device is flashed.
Display checks also validate daily values, solid fills, weekday colors, and
offline calendar-boundary transitions in both layouts, portrait text contrast,
and landscape header placement and removed callouts. The 90 hashes in
`tests/display/landscape.sha256.json` preserve the revised landscape layout
using synthetic PPM renders (see the [landscape plan](../docs/develop-feature/landscape-usage-layout/plan.md)).
Keep these baselines unchanged for portrait-only work; update them only for an
intentional landscape change.
The auth-failure fixture names trigger Gitleaks' generic API-key heuristic.
`.gitleaks.toml` allows only their exact known checksum entries in that file;
publication tests verify that other values, files, and appended secrets still fail.
After changing firmware, also inspect the actual LCD, title-tap rotation, and
stale/recovery behavior on your device using the normal build/flash commands.

## Output and fixtures

The desktop, firmware, display, and touch runners clean up their temporary
outputs automatically. Add `--output artifacts/check/<name>` to retain a run;
the display runner includes PPM fixture images and a manifest. Retained outputs
can be deleted when no check is running. Go uses its normal compiler cache.

`contracts/validate.py` is the independent Python protocol oracle.
`contracts/generate_v2.py` regenerates the checked-in synthetic v2 fixtures:

```sh
.venv/bin/python tests/contracts/generate_v2.py
```

Review fixture changes before committing them. Publication scanner tests are in
`security/`; run them with Gitleaks available as described in CONTRIBUTING.md.
