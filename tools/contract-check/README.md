# Contract checks (development only)

From the repository root, using Python 3.11+ with an IANA timezone database:

```sh
python3 -m venv artifacts/contract-venv
artifacts/contract-venv/bin/python -m pip install -r tools/contract-check/requirements.txt
artifacts/contract-venv/bin/python tools/contract-check/validate.py
artifacts/contract-venv/bin/python -m unittest discover -s tools/contract-check -v
```

The first check verifies every fixture's expected acceptance/rejection category,
including structural schema, cross-field semantics, strict parsing and raw-byte
budget. Fixture bytes are the intended HTTP bodies; no trailing newline is
required. The tests cover projection over stale/reset/DST boundaries and guard
against accidental acceptance of inconsistent payloads. These two commands are
the mandatory CM-002 contract gates. To check one usage file:

```sh
artifacts/contract-venv/bin/python tools/contract-check/validate.py path/to/usage.json
```

Use `--kind health` or `--kind error` for the other envelopes. No network, account
credentials, collector or container is needed once development dependencies are
installed. Port this contract into the runtime's language in downstream stories;
do not package this development tool or Python into the resource-limited service.
