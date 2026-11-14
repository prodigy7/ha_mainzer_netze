"""Constants for the Mainzer Netze integration.

See PROTOCOL.md in the repo root for the portal API these constants are based on.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

DOMAIN = "mainzer_netze"

# --- Config entry keys ---
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# --- Portal ---
BASE_URL = "https://msb.mainzer-netze.de/ems/swmn/fo/portal"
# The portal works in local German time; all value ranges are requested in it.
PORTAL_TZ = ZoneInfo("Europe/Berlin")

# --- Timing / politeness ---
# New data arrives once a day (~07:00 local, covering up to the previous
# midnight). Hourly polling is plenty, and the coordinator skips network
# access entirely once yesterday's data has been imported.
UPDATE_INTERVAL = timedelta(hours=1)
# Minimum gap between two HTTP requests of the same client, so a backfill
# never bursts requests at the portal.
MIN_REQUEST_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
# Delays before retrying a transient failure (5xx, timeout, network error).
# One entry per retry; live-observed: a single sporadic 503 that succeeded on retry.
RETRY_DELAYS_SECONDS: tuple[float, ...] = (5.0, 15.0)
# Max days per valueRequest. 31 days (~450 kB JSON) was live-verified to work,
# 14 days keeps responses at ~200 kB.
MAX_DAYS_PER_REQUEST = 14
# On each new portal import, re-fetch this many days before the last imported
# statistic, so late corrections/gap fills by the operator are picked up.
REFETCH_DAYS = 2

# --- Portal API vocabulary (see PROTOCOL.md) ---
GRANULARITY_QUARTER_HOUR = "QUARTER_HOUR"
AGGREGATION_SUM = "SUM"
DATE_TYPE_INDIVIDUAL = "INDIVIDUAL"
INDICATOR_TYPE_METER = "METER"
