# Contributing

Use synthetic account data in issues, tests, screenshots, and documentation.
Read [SECURITY.md](SECURITY.md) before sharing logs or building release assets.

## Development setup

Use a C++17 compiler, Python 3.12–3.14, and Go 1.26.8 or a newer patched release.
The collector and API have no third-party Go modules. Python dependencies are
for development and firmware builds.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r tools/contract-check/requirements.txt
```

The Go module's minimum version allows Go's toolchain selection to download a
compatible compiler. For offline builds, install that compiler first.

## Validation

Run from the repository root. These checks use synthetic data and temporary
loopback servers. They do not contact Codex, flash hardware, or change services.

```sh
.venv/bin/python tools/api-check/check.py --output artifacts/check/backend
.venv/bin/python -m unittest discover -s tools/lifecycle-check
.venv/bin/python -m unittest discover -s tools/acceptance-check
.venv/bin/python tools/firmware-check/check.py
.venv/bin/python tools/firmware-check/config.py
.venv/bin/python tools/display-check/check.py --output artifacts/check/display
.venv/bin/python tools/display-check/check_touch.py --output artifacts/check/touch
```

The backend wrapper includes formatting, race tests, `go vet`, native builds,
both contract versions, CLI privacy checks, and API/history validation. Firmware
checks use AddressSanitizer and UndefinedBehaviorSanitizer. Hardware acceptance
requires a separate local run following the tool READMEs; keep its evidence private.

## Publication checks

Install the pinned scanner and the current Go vulnerability checker:

```sh
go install github.com/zricethezav/gitleaks/v8@v8.30.1
go install golang.org/x/vuln/cmd/govulncheck@latest
export PATH="$(go env GOPATH)/bin:$PATH"
python3 scripts/check_secrets.py
python3 -m unittest discover -s tools/security-check
govulncheck ./...
(cd tools/collector-probe && govulncheck ./...)
```

The publication script scans tracked files and non-ignored new files, rejects
private artifact paths and personal home paths, flags email addresses for review,
and runs Gitleaks on both the candidate and existing Git history. It never copies
ignored local secrets into the scan directory. Review the actual staged diff too:
scanners cannot distinguish every real usage observation from a synthetic value.

Configure your GitHub noreply email locally before committing. In GitHub account
settings, find the address under **Emails**, then use `git config --local user.email`
with that address. Changing this setting does not update existing commits.

CI runs secret and Go vulnerability scans, backend checks, lifecycle tests, and
native firmware/display checks. No account secrets or device access are required.
