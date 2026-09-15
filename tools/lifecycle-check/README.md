# Installation lifecycle validation

Run from the repository root with Python 3.11+. No test reads or modifies native
Codex credentials. Unit tests use repository-local temporary directories; the
integration test uses a nonexistent credential path and a synthetic retained
80% reading with a known test installation key.

```sh
python3 -m unittest discover -s tools/lifecycle-check
python3 tools/lifecycle-check/integration.py --output artifacts/cm-008/my-new-run
.venv/bin/python tools/api-check/check.py --output artifacts/cm-008/backend-check
```

The real integration requires macOS GUI launchd, Go and running Docker Desktop.
Choose a new output path and an unused loopback port (`--port`, default 18088).
It installs only repository-contained temporary LaunchAgent files, runs start
twice, checks the native duplicate lock, preserves the same key/observation/
cycle through stop/start, kills only its collector job, removes only its API
container, observes automatic recovery, and runs uninstall/reinstall. Cleanup
keeps the test configuration/history as ignored evidence and removes its jobs.
It also verifies an unrelated sentinel remains and that no credential directory
was created. Events contain fixed labels and timestamps; CLI logs are sanitized.

`reconcile-baseline.json` records five warm no-change API checks using macOS
`/usr/bin/time -l`. Wall/user/system time includes Docker CLI work; maximum RSS
is the largest individual process, not a simultaneous aggregate. This initial
lifecycle baseline complements the collector/API measurements. CM-010 owns the
15-minute combined process budget and actual sleep/wake plus 24-hour CYD soak.
Existing collector tests exercise credential replacement, retry recovery and
atomic history persistence; CM-006/007 retain physical Wi-Fi/API recovery evidence.

Before staging, also check ignored private paths and tracked examples with
`git check-ignore`, inspect installation-guide links and commands, and run
`git diff --check`. No remote submission-created gates exist for this local PoC.
