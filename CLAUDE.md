# CLAUDE.md – Mainzer Netze HA integration

Home Assistant custom integration (`custom_components/mainzer_netze`, HACS-ready)
that imports quarter-hour smart meter data from the Mainzer Netze portal
(msb.mainzer-netze.de) as **external long-term statistics**. The portal has no
public API; **read `PROTOCOL.md` before touching `api.py`.**

## Hard rules

- **English only** in code, identifiers, comments, docs. German only in `translations/de.json`.
- **Read-only towards the portal.** Never call state-changing endpoints (see PROTOCOL.md, last section).
- **Be gentle with the portal**: keep the request throttle (`MIN_REQUEST_INTERVAL_SECONDS`), never retry
  rejected credentials (captcha risk), no extra requests per update cycle. Live testing only via
  `scripts/test_live.py` (~8 requests), never loops/bursts.
- **No personal data in the repo**: credentials only in `.env` (gitignored); test fixtures must stay
  anonymized (fake name, `DE0000…0001` metering point, `00000000-…` UUIDs).
- **`statistic_id` format is a public contract** (`statistic_id_for`); changing it orphans user history.
- **Keep docs in sync**: user-facing behavior → `README.md` (entities, examples, robustness);
  dev workflow → `DEVELOPMENT.md`; portal facts → `PROTOCOL.md`.

## Layout

| File | Responsibility |
|---|---|
| `api.py` | aiohttp client, **no HA imports**. Login/logout, throttle, retries, structural response validation + one re-login, pure parsers (`parse_*`, `split_range`, `build_value_request`) |
| `statistics.py` | Readings → hourly buckets (UTC) → cumulative `StatisticData`; recorder lookups/import |
| `coordinator.py` | Update cycle: skip if data current → login → initData per meter → fetch window → chunked import → logout. Maps errors to `ConfigEntryAuthFailed` / `UpdateFailed` |
| `config_flow.py` | User step (validates login + ≥1 meter), reauth. unique_id = lowercased username |
| `sensor.py` | Summary sensors only (last complete day, data until, last import) |
| `diagnostics.py` | Redacts credentials, names, metering point id, statistic ids |
| `const.py` | All tunables (intervals, chunk size, retry delays, refetch days) |

## Commands

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest tests/ -q                      # full suite, ~15 s, no network
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy custom_components/mainzer_netze --ignore-missing-imports --check-untyped-defs
.venv/bin/python scripts/test_live.py [--days N] [--debug]   # real portal, needs .env
```

CI (`.github/workflows/`) runs ruff, mypy, pytest, hassfest and HACS validation.

## Key facts and pitfalls

- **Expired session ≠ 401**: tree → HTTP 311, initData → HTTP 200 `{}`, valueRequest → HTTP 503.
  Parsers raise `_SessionInvalidError` on unexpected shape → exactly one re-login, then `MainzerNetzeApiError`.
  5xx/timeouts → `_TransientError`, retried per `RETRY_DELAYS_SECONDS` (with re-login).
- **Timing**: data arrives once a day (~07:00) up to previous midnight. `_is_data_current()` skips all
  network access once `measured_end >= today 00:00` local. Don't add per-cycle requests.
- **Statistics**: hourly sums keyed by UTC hour; each cycle re-imports from `REFETCH_DAYS` before the last
  imported hour, sum base = `async_get_sum_before(window_start)`. Backfill imports chunk by chunk so an
  interrupted run resumes. The day-total sensor has **no `state_class`** on purpose (HA would misdate it).
- **DST/datetime traps** (both have bitten here): datetimes sharing one `ZoneInfo` compare/subtract in
  wall-clock time and ignore `fold` → normalize to UTC before subtracting, comparing or using as dict keys.
  Wall-clock arithmetic on `ZoneInfo` datetimes is fine for "same local time N days earlier".
  Portal TZ is fixed `Europe/Berlin` (`PORTAL_TZ`), independent of HA's TZ.
- **HA ≥ 2026.10 idioms**: `probatio` (not voluptuous) for schemas, `entry.runtime_data`,
  `StatisticMetaData(mean_type=…, unit_class=…)`, `async_create_clientsession(hass)` per entry
  (own cookie jar – required, the portal uses a session cookie).

## Tests

- `tests/fake_portal.py`: in-process aiohttp fake of the portal incl. all quirks above; client tests hit it via
  real HTTP (`portal_server` fixture needs `socket_enabled`).
- `tests/helpers.py`: `FakeClient` (above HTTP), `HA_FIXTURES` – HA-level modules set `pytestmark = HA_FIXTURES`
  (recorder must start before `hass`).
- `tests/fixtures/*.json`: real, anonymized portal responses (day, DST switch, tree, initData).
- New behavior → test first in the matching module; portal quirks → extend the fake portal, don't mock `_send`.

## Open points / not verified

- Only one account with one consumption meter was available: multi-meter, nested groups and feed-in
  features are implemented generically but untested live.
- Failed-login and captcha response shapes come from the frontend JS (deliberately not provoked live).
- Response for a value range without data is unverified (parser treats missing/empty `values` as no data).
