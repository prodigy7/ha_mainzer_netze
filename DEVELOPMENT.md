# Development guide

For end-user installation and features, see [README.md](README.md). This document is for
working on the integration itself.

## Project structure

```
.
├── .github/workflows/                 # CI: ruff, mypy, pytest, hassfest/HACS validation
├── CLAUDE.md                          # Context & project brief for Claude Code
├── PROTOCOL.md                        # Reverse-engineered portal API documentation
├── hacs.json                          # HACS metadata
├── pytest.ini                         # pytest config
├── ruff.toml                          # Lint/format config
├── requirements_test.txt              # Test dependencies (pins HA via pytest-homeassistant-custom-component)
├── scripts/
│   ├── _env.py                        # .env loader (no shell involved)
│   └── test_live.py                   # Read-only live check against the real portal
├── tests/                             # Offline tests, no network access (see below)
└── custom_components/
    └── mainzer_netze/
        ├── __init__.py                # Config entry setup/unload
        ├── api.py                     # Async portal client (no HA imports)
        ├── config_flow.py             # Setup + reauth via the HA UI
        ├── const.py                   # Domain, URLs, timing/politeness tunables
        ├── coordinator.py             # DataUpdateCoordinator: fetch + statistics import
        ├── diagnostics.py             # Redacted diagnostics export
        ├── manifest.json
        ├── sensor.py                  # Summary sensors (last day, data until, last import)
        ├── statistics.py              # Hourly aggregation, recorder import
        ├── strings.json               # UI text (source for translations)
        └── translations/
            ├── en.json
            └── de.json
```

## Getting started

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
cp .env.example .env   # fill in portal credentials, only needed for scripts/test_live.py
```

Then read `CLAUDE.md` and `PROTOCOL.md`.

## Tests and validation

```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy custom_components/mainzer_netze --ignore-missing-imports --check-untyped-defs
```

All three run in CI on every push/PR, together with hassfest and HACS validation.

- **`tests/test_api_*.py`**: the portal client, against `tests/fake_portal.py` -- an in-process
  aiohttp server reproducing the portal's live-observed behavior (login flow, HTTP 311/`{}`/503
  for expired sessions, captcha, redirects). Portal quirks belong there, not in mocks.
- **`tests/test_statistics.py`, `test_coordinator.py`, `test_config_flow.py`, `test_init.py`**:
  Home Assistant level, using `pytest-homeassistant-custom-component` with a real recorder and
  `tests/helpers.py`'s `FakeClient`.
- **`tests/fixtures/`**: real portal responses, **anonymized**. Keep it that way when adding new
  ones (no names, metering point IDs, or account UUIDs).

## Live check against the real portal

```bash
.venv/bin/python scripts/test_live.py            # newest day
.venv/bin/python scripts/test_live.py --days 7 --debug
```

Read-only: login, meters, meter details, readings, logout -- about 8 requests. Please keep live
testing at this scale: no loops, no load tests, and never repeated logins with wrong
credentials (the portal then demands a captcha, which has to be solved manually in a browser).

## Working with Claude Code (or another AI assistant)

This integration was built with Claude Code. If you continue that way:

- **`CLAUDE.md` is picked up automatically** as project context. It contains the hard rules
  (English only, read-only towards the portal, gentle request behavior, no personal data in
  the repo), the architecture, and the known pitfalls. Just state the task.
- **Point it at `PROTOCOL.md` for API questions** instead of describing the portal yourself.
  Static analysis of the portal's frontend bundle
  (`/ems/swmn/fo/portal/resources/js/dist/NextAnalysis.js`) found every endpoint used here
  with only a few live requests -- prefer it over trial and error against the portal.
- **Insist on live verification** for anything touching `api.py` (`scripts/test_live.py`),
  and ask for the actual output.
- **Review `PROTOCOL.md` changes carefully**: it documents undocumented behavior; a
  plausible-sounding wrong entry is worse than an admitted gap.
- **Environment gotchas**:
  - Never load `.env` via `source .env` in Bash -- special characters in the password (`$`,
    `%`, `*`) get interpreted by the shell. `scripts/_env.py` parses the file directly.
  - `pytest-homeassistant-custom-component` blocks sockets; tests needing the local fake portal
    use the `socket_enabled` fixture (already wired into the `portal_server` fixture).
  - HA-level test modules need `pytestmark = HA_FIXTURES` (the recorder must start before
    `hass`).

## Releasing

1. Bump `version` in `custom_components/mainzer_netze/manifest.json`.
2. Make sure CI is green.
3. Create a GitHub release with a tag matching the version (HACS picks up releases).
