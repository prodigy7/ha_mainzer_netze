# Mainzer Netze metering portal – reverse-engineered API

The portal at <https://msb.mainzer-netze.de/> ("Energiemanagement Portal", the
metering point operator portal of Mainzer Netze) has no public API. Everything
below was derived from the portal's own web frontend (static analysis of
`resources/js/dist/NextAnalysis.js`) and verified live on 2026-11-14 against
one account with a single electricity meter.

Base URL for all paths below: `https://msb.mainzer-netze.de/ems/swmn/fo/portal`

## Data availability

- Granularity: quarter-hours (`nativeGranularityType: QUARTER_HOUR`), unit kWh,
  three decimals.
- The operator imports data **once a day**, observed at ~07:09 local time
  (`lastImportDate`). An import covers everything up to the **previous
  midnight**. So the newest value is between ~7 and ~31 hours old.
- History reaches back to when the smart meter was installed
  (`measuredValueRange`, here from 2025-07-23).

## Authentication

Session-cookie based (`JSESSIONID`, path `/ems/swmn/fo`). No CSRF token.

1. `GET /start` – sets a fresh `JSESSIONID`.
2. `POST /loginWidget.json` (form-encoded `login`, `password`, header
   `X-Requested-With: XMLHttpRequest`) – validation only.
   - Success: HTTP 200
     `{"model": {"login": "...", "password": "", "captchaRequired": false, "captcha": "", ...}}`
   - Failure (from the frontend JS, deliberately **not** provoked live): non-2xx
     with `{"model": {...}, "hasGlobalErrors": true, "globalerrors": [...],
     "hasFieldErrors": ..., "fielderrors": {...}}` or `{"exception": "..."}`.
   - `captchaRequired: true` means the portal wants a captcha (typically after
     failed logins). Only a manual browser login resolves that. **Never retry
     credentials automatically.**
3. `POST /loginProcess` (same form fields) – the real login. Redirects to
   `/home` on success. A response that ends on `/start` or contains the login
   form (`id="loginProcessForm"`) means failure.

Logout: `GET /logout` → HTTP 302 to `/start`, session invalidated.

### Expired / missing session – no 401!

| Endpoint | Response without valid session |
|---|---|
| `GET /indicatorGroupList.json` | HTTP **311** `{"exception": "Bei der Bearbeitung Ihrer Anfrage ist ein Fehler aufgetreten."}` |
| `POST /analysis/initData` | HTTP **200** `{ }` (silently empty) |
| `POST /analysis/valueRequest` | HTTP **503** HTML error page |

Clients must therefore validate the response *structure*, not only the status
code. A single sporadic HTTP 503 with a valid session was also observed once
(succeeded on retry), so 503 is treated as transient *and* as a possible
session loss (re-login before retrying).

## Endpoints used

All JSON requests: `Content-Type: application/json`,
`X-Requested-With: XMLHttpRequest`, `Accept: application/json`.

### `GET /indicatorGroupList.json` – object tree (meters)

```json
{
  "id": "<uuid>", "type": "rootIndicatorGroup", "text": "/",
  "children": [
    {"id": "<indicator uuid>", "type": "indicatorPOWER", "text": "<object name>",
     "locationNo": "DE00…<33-char metering point id>", "children": [], "state": {...}}
  ]
}
```

Node types (from the JS): `rootIndicatorGroup`, `indicatorGroup` (folders,
may nest), `indicator<MEDIUM>` (meters, e.g. `indicatorPOWER`).

### `POST /analysis/initData` – meter metadata

Request: `{"currentIndicatorID": "<indicator uuid>", "fetchLastImport": true}`

```json
{
  "featureDescriptions": {
    "POWER_ACTIVE_PURCHASE_CONSUMPTION": {
      "displayName": "(+) Elektrizität: Wirkenergie",
      "unit": {"unit": "(W.h).1000", "displayName": "kWh"},
      "media": {"mediaType": "POWER", "displayName": "Elektrizität"}
    }
  },
  "currentIndicator": {
    "indicatorID": "<uuid>", "indicatorType": "METER", "displayName": "<object name>",
    "locationNo": "DE00…", "lastImportDate": "2026-11-13T07:08:59.571+01:00",
    "features": {
      "POWER_ACTIVE_PURCHASE_CONSUMPTION": [{
        "nativeGranularityType": "QUARTER_HOUR", "aggregated": false,
        "measuredValueRange": {"2025-07-23T00:00:00.000+02:00": "2026-11-13T00:00:00.000+01:00"}
      }]
    }
  }
}
```

A "feature" is a measurement series. Only the consumption feature was seen;
accounts with PV feed-in very likely get a second (supply) feature.

### `POST /analysis/valueRequest` – the actual values

```json
{
  "dateType": "INDIVIDUAL",
  "userTimeZone": "Europe/Berlin",
  "indicatorValueRequests": [{
    "valueRange": {"2026-11-12T00:00:00.000+01:00": "2026-11-13T00:00:00.000+01:00"},
    "indicatorID": "<uuid>", "indicatorType": "METER", "index": 0,
    "featureValueRequests": [{
      "featureID": "POWER_ACTIVE_PURCHASE_CONSUMPTION",
      "granularityType": "QUARTER_HOUR", "aggregationType": "SUM"
    }]
  }]
}
```

Response:

```json
{"indicatorValueResponses": [{
  "indicatorID": "<uuid>", "indicatorType": "METER", "index": 0,
  "featureValueResponses": [{
    "featureID": "POWER_ACTIVE_PURCHASE_CONSUMPTION", "aggregationType": "SUM",
    "granularityType": "QUARTER_HOUR", "valueRange": {...},
    "values": [
      {"value": 0.093, "measuringPeriod": {"2026-11-12T00:00:00.000+01:00": "2026-11-12T00:15:00.000+01:00"}}
    ],
    "totalValues": {"SUM": {"value": 12.132, "measuringPeriod": {...}}}
  }]
}]}
```

- Intervals are `{start: end}` single-entry dicts with explicit UTC offsets.
  DST is handled correctly: 2026-10-25 has 100 quarter-hours, the repeated
  hour appears as `…02:45+02:00 → 02:00+01:00`.
- `value` may be `null` for missing intervals (frontend option
  `fillMissingValuesWithNull`).
- Range sizes live-verified: 1, 2, 7, 14 and 31 days (31 days ≈ 2980 values,
  ≈ 450 kB, fast). The integration uses 14-day chunks.
- Enum values (from the JS): `aggregationType` SUM/MIN/MAX/AVG/MEDIAN/NOT_SET;
  `dateType` DAY/WEEK/MONTH/QUARTER/YEAR/YEARS_3/YEARS_5/INDIVIDUAL/COMPLETE.
- Unverified: the exact response for a range without any data yet.

## Raw CSV export (not used)

The UI's "Rohdaten als CSV-Datei" does `POST /analysis/rawCsvRequest` (same
payload as `valueRequest`) → `{"downloadUrl": "analysis/csvDownload"}` →
`GET /analysis/csvDownload` (session-bound file). The CSV is worse than the
JSON: values rounded to **2** decimals, ISO-8859-1, `;`-separated, local
timestamps **without offset** (ambiguous in the DST night), and it needs an
extra stateful request. Columns:
`Objektbezeichnung;ID;Messreihenbezeichnung;Beginn der Messung;Ende der Messung;Messwert;Einheit`.

## Endpoints deliberately not used

The frontend also knows state-changing endpoints, e.g. `bookmarkSave`,
`setContext.json`, `userUpdate`, `userPasswordUpdate`,
`centralParameterMaintenanceTree`. This integration is strictly read-only and
must never call them.
