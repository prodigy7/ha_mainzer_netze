# Home Assistant Integration: Mainzer Netze

Native Home Assistant integration for the smart meter data of
[Mainzer Netze](https://www.mainzer-netze.de/), fetched from their metering portal
(<https://msb.mainzer-netze.de/>) via its reverse-engineered internal web API. Quarter-hour
consumption values are imported as long-term statistics with their real timestamps, ready for
the **Energy dashboard** -- similar to what the official Tibber integration does.

> Unofficial. Not affiliated with Mainzer Netze GmbH. The portal's internal API may change
> without notice.

## Installation

**Via HACS:**

1. In HACS, add this repository as a custom repository:
   `https://github.com/prodigy7/ha_mainzer_netze`, category "Integration".
2. Install "Mainzer Netze" from HACS, then restart Home Assistant.
3. Continue with step 3 below ("Add Integration").

**Manual install:**

1. Copy `custom_components/mainzer_netze/` into your Home Assistant config directory, so you
   end up with `<config>/custom_components/mainzer_netze/`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration**, search for "Mainzer Netze", and
   enter:
   - **Username** of your portal account (usually your e-mail address)
   - **Password**
4. That's it -- all meters assigned to the account are discovered automatically. If the
   credentials are wrong, the portal is unreachable, or no meter is assigned to the account,
   the form shows an error and nothing is saved.

Requires Home Assistant 2026.10 or newer.

On first setup the complete available history (since your smart meter was installed) is
backfilled into the statistics. This takes about a minute for 1.5 years of data -- requests are
deliberately spaced out so the portal isn't flooded. Afterwards no further configuration is
needed; see "How data is fetched" below for the update behavior.

## Status

Core functionality is implemented and verified live against the real portal: login/logout,
meter discovery, meter metadata (series, units, measured range, last import), quarter-hour value
retrieval, hourly aggregation into long-term statistics, and three sensors per meter. Values
match the portal's own daily totals exactly (e.g. 12.132 kWh for a sample day).

This integration is strictly **read-only**: it only reads data the portal's own analysis page
shows. State-changing portal functions (bookmarks, user/password updates, parameter
maintenance) are intentionally never called. See `PROTOCOL.md` for the full API documentation
and `CLAUDE.md` for the project brief.

### Entities

Per meter, one device "Mainzer Netze \<object name\>" with:

| Entity | Description |
|---|---|
| `sensor.*_last_day` (one per series, e.g. "(+) Elektrizität: Wirkenergie last day") | Total of the most recent *complete* day, attribute `date` says which day, attribute `statistic_id` names the matching statistic |
| `sensor.*_data_available_until` | End of the newest value available on the portal |
| `sensor.*_last_portal_import` | When the operator last imported data (diagnostic) |

Plus one external statistic per series, e.g.
`mainzer_netze:de0000…0001_power_active_purchase_consumption` (hourly sums in kWh).

### Usage examples

Entity IDs below assume an object name "Example Customer" in the portal and are only
illustrative -- check **Settings → Devices & Services → Mainzer Netze → Entities** for your
actual IDs. The `statistic_id` attribute of the `*_last_day` sensor shows your exact statistic
ID.

**Energy dashboard:** **Settings → Dashboards → Energy → Electricity grid → Add consumption**,
then pick the statistic "\<object name\> (+) Elektrizität: Wirkenergie". Use the statistic, not
the `*_last_day` sensor.

**Hourly consumption chart** (statistics graph card on any dashboard):

```yaml
type: statistics-graph
title: Grid consumption (Mainzer Netze)
entities:
  - mainzer_netze:de0000000000000000000000000000001_power_active_purchase_consumption
stat_types:
  - change
period: hour
days_to_show: 7
chart_type: bar
```

**Daily consumption notification** once the portal has delivered yesterday's data (triggers on
the `date` attribute, so it fires exactly once per new day):

```yaml
automation:
  - alias: "Notify daily grid consumption"
    trigger:
      - trigger: state
        entity_id: sensor.mainzer_netze_example_customer_elektrizitat_wirkenergie_last_day
        attribute: date
    condition:
      - condition: template
        value_template: "{{ trigger.from_state is not none and trigger.to_state.attributes.date is not none }}"
    action:
      - action: notify.notify
        data:
          message: >
            Grid consumption on {{ trigger.to_state.attributes.date }}:
            {{ trigger.to_state.state | float | round(2) }} kWh.
```

**Estimated cost of the last day** (template sensor; adjust the price or pull it from an
`input_number`):

```yaml
template:
  - sensor:
      - name: "Grid cost last day"
        unit_of_measurement: "€"
        state: >
          {% set kwh = states('sensor.mainzer_netze_example_customer_elektrizitat_wirkenergie_last_day') %}
          {{ (kwh | float(0) * 0.32) | round(2) if kwh not in ['unknown', 'unavailable'] else none }}
```

**Alert when the portal stops delivering data** (normally the newest value is at most ~31 hours
old):

```yaml
automation:
  - alias: "Mainzer Netze data is stale"
    trigger:
      - trigger: template
        value_template: >
          {{ now() - states('sensor.mainzer_netze_example_customer_data_available_until') | as_datetime
             > timedelta(days=3) }}
    action:
      - action: notify.notify
        data:
          message: "The Mainzer Netze portal has not delivered new meter data for 3 days."
```

### How data is fetched

The operator imports meter data **once a day** (around 07:00), covering everything up to the
previous midnight -- so the newest value is always 7 to 31 hours old. That is a property of the
portal, not of this integration.

- Every hour, the integration checks whether the portal has data beyond what's already
  imported. Once yesterday's data has arrived, it **doesn't contact the portal again until the
  next day** -- in practice just a handful of small requests per day.
- With each new import, the last two days are re-fetched and re-imported, so late corrections
  or gap fills by the operator are picked up.
- Each update cycle logs in, fetches, and logs out again; requests are spaced at least one
  second apart.

### Why statistics instead of a regular sensor

A regular sensor with a `state_class` would make Home Assistant attribute yesterday's
consumption to the moment the value arrived (e.g. today 07:00) and produce wrong hourly
statistics. Therefore the time series is written directly as external statistics with the real
timestamps (quarter-hours summed into hours, the finest resolution of HA's long-term
statistics), and the `*_last_day` sensor intentionally has **no** `state_class`. Don't add one
via customization, and don't use that sensor in the Energy dashboard.

### Robustness

Verified by the test suite against an in-process fake of the portal that reproduces its
live-observed quirks:
- **Portal unreachable when HA starts**: raises `ConfigEntryNotReady`, so HA retries setup
  automatically with backoff.
- **Temporary errors during operation** (timeouts, HTTP 5xx): retried twice with delays; if
  the portal stays down, entities become unavailable and recover on the next successful cycle.
  Imported statistics are not affected.
- **Expired session**: the portal doesn't answer with HTTP 401 but with empty or malformed
  responses -- every response is validated structurally, and the integration logs in again
  once transparently.
- **Interrupted backfill**: data is imported chunk by chunk; the next cycle resumes where it
  stopped instead of starting over.
- **Password changed after setup**: triggers Home Assistant's standard reauth flow (a
  notification leading to a small "enter new password" form). A rejected password is **never
  retried automatically**, since repeated failed logins make the portal demand a captcha.
- **Captcha required**: shown as a dedicated error. Log in once in your browser at
  msb.mainzer-netze.de, then retry.
- **Daylight saving time**: the 23- and 25-hour days are handled correctly (verified with real
  portal data from the October switch).

### Known limitation: tested with a single consumption meter only

**Everything in this repo has only been verified live against one account with a single
electricity consumption meter.** The code handles multiple meters per account, nested meter
groups, and additional series (e.g. PV feed-in) generically, but none of that has been tested
against a real portal account. Series with units other than Wh/kWh/MWh/m³ are skipped with a
log warning. If your account has more meters or a feed-in series, please report back what you
find -- `PROTOCOL.md` documents the API and what is still unverified.

### Debugging

Standard Home Assistant debug logging works out of the box (Settings → Devices & Services →
this integration → "..." → *Enable debug logging*, or via `logger:` in `configuration.yaml`
for `custom_components.mainzer_netze`). This traces the login lifecycle, every request (method,
path, status, size -- credentials are never logged), re-logins, retries, and which ranges are
fetched. **Download diagnostics** on the integration page exports meter metadata and the last
update state, with credentials, names, and metering point IDs redacted.

## For developers

Working on the integration itself (project structure, tests, live check, validation before
PRs)? See [DEVELOPMENT.md](DEVELOPMENT.md).

## License

MIT, see [LICENSE](LICENSE).
