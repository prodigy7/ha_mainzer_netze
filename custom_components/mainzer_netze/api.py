"""Async client for the Mainzer Netze metering portal (msb.mainzer-netze.de).

The portal has no public API. This client talks to the same JSON endpoints the
portal's own web frontend uses (reverse-engineered, see PROTOCOL.md). It has
no Home Assistant dependencies so it can be tested and used standalone.

Key portal quirks this client handles:
- An expired/missing session is NOT signalled with 401. Depending on the
  endpoint it shows up as HTTP 200 with an empty `{}`, HTTP 311 with
  `{"exception": ...}`, an HTML page, or HTTP 503. Every response is therefore
  validated structurally; an invalid one triggers exactly one re-login.
- Failed logins can make the portal require a captcha. Credentials are never
  retried automatically after a rejected login.
- Requests are serialized and spaced out (MIN_REQUEST_INTERVAL_SECONDS) so the
  portal is never flooded, even during a large backfill.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

import aiohttp

from .const import (
    AGGREGATION_SUM,
    BASE_URL,
    DATE_TYPE_INDIVIDUAL,
    GRANULARITY_QUARTER_HOUR,
    MAX_DAYS_PER_REQUEST,
    MIN_REQUEST_INTERVAL_SECONDS,
    PORTAL_TZ,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_DELAYS_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

PATH_START = "/start"
PATH_LOGIN_VALIDATE = "/loginWidget.json"
PATH_LOGIN_PROCESS = "/loginProcess"
PATH_LOGOUT = "/logout"
PATH_TREE = "/indicatorGroupList.json"
PATH_INIT_DATA = "/analysis/initData"
PATH_VALUES = "/analysis/valueRequest"

_AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
# Marker of the login form; present in a response means "not logged in".
_LOGIN_FORM_MARKER = 'id="loginProcessForm"'

TREE_TYPE_ROOT_GROUP = "rootIndicatorGroup"
TREE_TYPE_GROUP = "indicatorGroup"
TREE_TYPE_INDICATOR_PREFIX = "indicator"


# --- Exceptions ---


class MainzerNetzeError(Exception):
    """Base class for all errors raised by this client."""


class MainzerNetzeAuthError(MainzerNetzeError):
    """The portal rejected the credentials."""


class MainzerNetzeCaptchaRequiredError(MainzerNetzeAuthError):
    """The portal requires a captcha (usually after failed login attempts).

    Can only be resolved by logging in once manually via the web browser.
    """


class MainzerNetzeConnectionError(MainzerNetzeError):
    """Network problem, timeout, or persistent server-side (5xx) error."""


class MainzerNetzeApiError(MainzerNetzeError):
    """The portal answered with something this client does not understand."""


class _SessionInvalidError(MainzerNetzeApiError):
    """Response looks like a missing/expired session (internal, triggers re-login)."""


class _TransientError(MainzerNetzeConnectionError):
    """A single failed attempt that is worth retrying (internal)."""


# --- Data model ---


@dataclass(frozen=True)
class Meter:
    """A meter (portal: "indicator") from the portal's object tree."""

    indicator_id: str
    name: str
    location_no: str
    media_type: str  # e.g. "POWER", derived from the tree node type


@dataclass(frozen=True)
class Feature:
    """A measurement series of a meter, e.g. consumption or feed-in."""

    feature_id: str  # e.g. "POWER_ACTIVE_PURCHASE_CONSUMPTION"
    display_name: str  # e.g. "(+) Elektrizität: Wirkenergie"
    unit: str  # display unit as sent by the portal, e.g. "kWh"
    native_granularity: str | None
    measured_start: datetime | None
    measured_end: datetime | None


@dataclass(frozen=True)
class MeterDetails:
    """Meter metadata from analysis/initData."""

    indicator_id: str
    indicator_type: str
    name: str
    location_no: str
    last_import: datetime | None
    features: dict[str, Feature] = field(default_factory=dict)


@dataclass(frozen=True)
class Reading:
    """One measured interval (usually a quarter-hour), value in the feature's unit."""

    start: datetime
    end: datetime
    value: float


# --- Pure parsing helpers (no I/O, unit-tested directly) ---


def _parse_datetime(raw: Any) -> datetime | None:
    """Parses a portal timestamp like "2026-11-12T00:15:00.000+01:00" (must be tz-aware)."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parse_period(raw: Any) -> tuple[datetime, datetime] | None:
    """Parses the portal's single-entry {start: end} interval dict."""
    if not isinstance(raw, dict) or len(raw) != 1:
        return None
    ((start_raw, end_raw),) = raw.items()
    start, end = _parse_datetime(start_raw), _parse_datetime(end_raw)
    if start is None or end is None or end <= start:
        return None
    return start, end


def format_portal_datetime(value: datetime) -> str:
    """Formats an aware datetime the way the portal frontend does (ms precision)."""
    return value.astimezone(PORTAL_TZ).isoformat(timespec="milliseconds")


def parse_meters(tree: Any) -> list[Meter]:
    """Extracts all meters from the indicatorGroupList.json tree (recursively)."""
    if not isinstance(tree, dict) or not isinstance(tree.get("children"), list):
        raise _SessionInvalidError("Object tree response has no 'children' list")

    meters: list[Meter] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if (
            isinstance(node_type, str)
            and node_type.startswith(TREE_TYPE_INDICATOR_PREFIX)
            and node_type not in (TREE_TYPE_GROUP, TREE_TYPE_ROOT_GROUP)
            and isinstance(node.get("id"), str)
        ):
            meters.append(
                Meter(
                    indicator_id=node["id"],
                    name=str(node.get("text") or node["id"]),
                    location_no=str(node.get("locationNo") or ""),
                    media_type=node_type[len(TREE_TYPE_INDICATOR_PREFIX) :],
                )
            )
        for child in node.get("children") or []:
            walk(child)

    walk(tree)
    return meters


def parse_meter_details(data: Any, indicator_id: str) -> MeterDetails:
    """Parses an analysis/initData response for the given meter."""
    indicator = data.get("currentIndicator") if isinstance(data, dict) else None
    if not isinstance(indicator, dict) or indicator.get("indicatorID") != indicator_id:
        # An expired session yields HTTP 200 with "{}" here (live-verified).
        raise _SessionInvalidError("initData response has no matching 'currentIndicator'")

    descriptions = data.get("featureDescriptions")
    if not isinstance(descriptions, dict):
        descriptions = {}

    features: dict[str, Feature] = {}
    raw_features = indicator.get("features")
    for feature_id, series_list in (raw_features if isinstance(raw_features, dict) else {}).items():
        description = descriptions.get(feature_id) or {}
        unit = (description.get("unit") or {}).get("displayName")
        series = series_list[0] if isinstance(series_list, list) and series_list else {}
        if not isinstance(series, dict):
            series = {}
        measured = _parse_period(series.get("measuredValueRange"))
        features[feature_id] = Feature(
            feature_id=feature_id,
            display_name=str(description.get("displayName") or feature_id),
            unit=str(unit or ""),
            native_granularity=series.get("nativeGranularityType"),
            measured_start=measured[0] if measured else None,
            measured_end=measured[1] if measured else None,
        )

    return MeterDetails(
        indicator_id=indicator_id,
        indicator_type=str(indicator.get("indicatorType") or "METER"),
        name=str(indicator.get("displayName") or indicator_id),
        location_no=str(indicator.get("locationNo") or ""),
        last_import=_parse_datetime(indicator.get("lastImportDate")),
        features=features,
    )


def parse_readings(data: Any, indicator_id: str, feature_id: str) -> list[Reading]:
    """Parses an analysis/valueRequest response into readings, sorted by start.

    Missing (null) values are skipped; malformed or negative entries are
    skipped with a warning instead of poisoning the cumulative statistics.
    """
    responses = data.get("indicatorValueResponses") if isinstance(data, dict) else None
    if not isinstance(responses, list):
        raise _SessionInvalidError("valueRequest response has no 'indicatorValueResponses'")

    readings: list[Reading] = []
    for indicator in responses:
        if not isinstance(indicator, dict) or indicator.get("indicatorID") != indicator_id:
            continue
        for feature in indicator.get("featureValueResponses") or []:
            if not isinstance(feature, dict) or feature.get("featureID") != feature_id:
                continue
            for entry in feature.get("values") or []:
                reading = _parse_reading(entry)
                if reading is not None:
                    readings.append(reading)

    readings.sort(key=lambda r: r.start)
    return readings


def _parse_reading(entry: Any) -> Reading | None:
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if value is None:
        return None  # no value for this interval (yet)
    period = _parse_period(entry.get("measuringPeriod"))
    if (
        period is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        _LOGGER.warning("Skipping malformed reading from portal: %r", entry)
        return None
    if value < 0:
        _LOGGER.warning("Skipping negative reading from portal: %r", entry)
        return None
    return Reading(start=period[0], end=period[1], value=float(value))


def split_range(
    start: datetime, end: datetime, max_days: int = MAX_DAYS_PER_REQUEST
) -> list[tuple[datetime, datetime]]:
    """Splits [start, end) into chunks of at most max_days local calendar days.

    Chunk borders fall on local midnight (DST-safe, built from local dates);
    start/end themselves are kept as given.
    """
    if max_days < 1:
        raise ValueError("max_days must be >= 1")
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        border_date: date = cursor.astimezone(PORTAL_TZ).date() + timedelta(days=max_days)
        border = datetime.combine(border_date, datetime.min.time(), tzinfo=PORTAL_TZ)
        chunk_end = min(border, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


def build_value_request(
    details: MeterDetails, feature_id: str, start: datetime, end: datetime
) -> dict[str, Any]:
    """Builds the valueRequest payload exactly like the portal's analysis page."""
    return {
        "dateType": DATE_TYPE_INDIVIDUAL,
        "userTimeZone": str(PORTAL_TZ),
        "indicatorValueRequests": [
            {
                "valueRange": {format_portal_datetime(start): format_portal_datetime(end)},
                "indicatorID": details.indicator_id,
                "indicatorType": details.indicator_type,
                "index": 0,
                "featureValueRequests": [
                    {
                        "featureID": feature_id,
                        "granularityType": GRANULARITY_QUARTER_HOUR,
                        "aggregationType": AGGREGATION_SUM,
                    }
                ],
            }
        ],
    }


def _parse_login_error(body: str) -> MainzerNetzeError:
    """Maps a non-2xx loginWidget.json response to an exception."""
    try:
        data = json.loads(body)
    except ValueError:
        return MainzerNetzeApiError("Unexpected non-JSON login validation response")
    if not isinstance(data, dict):
        return MainzerNetzeApiError("Unexpected login validation response")
    model = data.get("model")
    if isinstance(model, dict) and model.get("captchaRequired"):
        return MainzerNetzeCaptchaRequiredError("Portal requires a captcha; log in once via the web browser")
    if data.get("hasGlobalErrors") or data.get("hasFieldErrors"):
        messages = list(data.get("globalerrors") or []) + list((data.get("fielderrors") or {}).values())
        return MainzerNetzeAuthError("; ".join(str(m) for m in messages) or "Login rejected")
    return MainzerNetzeApiError(f"Login validation failed: {data.get('exception') or data}")


# --- Client ---


class MainzerNetzeClient:
    """Session-based client for the portal.

    The aiohttp session must be dedicated to this client (own cookie jar),
    since the portal authenticates via a JSESSIONID cookie.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        base_url: str = BASE_URL,
        min_request_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._min_request_interval = min_request_interval
        self._retry_delays = retry_delays
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._request_lock = asyncio.Lock()
        self._last_request: float | None = None
        self._logged_in = False

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    # --- Low-level HTTP ---

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> tuple[int, str, str]:
        """One throttled HTTP request -> (status, body, final URL).

        Network errors, timeouts and 5xx are raised as _TransientError.
        """
        loop = asyncio.get_running_loop()
        async with self._request_lock:
            if self._last_request is not None:
                wait = self._min_request_interval - (loop.time() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                async with self._session.request(
                    method,
                    self._base_url + path,
                    json=json_body,
                    data=form,
                    headers=headers,
                    allow_redirects=allow_redirects,
                    timeout=self._timeout,
                ) as resp:
                    body = await resp.text(errors="replace")
                    status, final_url = resp.status, str(resp.url)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise _TransientError(f"{method} {path} failed: {err!r}") from err
            finally:
                self._last_request = loop.time()

        _LOGGER.debug("%s %s -> HTTP %s (%d bytes)", method, path, status, len(body))
        if status >= 500:
            raise _TransientError(f"{method} {path} returned HTTP {status}")
        return status, body, final_url

    async def _with_retries(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        """Runs operation, retrying _TransientError with the configured delays."""
        for attempt in range(len(self._retry_delays) + 1):
            try:
                return await operation()
            except _TransientError as err:
                if attempt >= len(self._retry_delays):
                    raise MainzerNetzeConnectionError(str(err)) from err
                delay = self._retry_delays[attempt]
                _LOGGER.debug("Transient portal error (%s), retrying in %ss", err, delay)
                # A 5xx can also be how the portal answers a lost session
                # (live-verified for valueRequest), so log in again on retry.
                self._logged_in = False
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _request_json(
        self, method: str, path: str, parse: Callable[[Any], _T], payload: Any = None
    ) -> _T:
        """Authenticated JSON request with structural validation and one re-login.

        `parse` raises _SessionInvalidError if the decoded response doesn't have
        the expected shape; that triggers one fresh login and a second try.
        """

        async def attempt() -> _T:
            relogged = False
            while True:
                if not self._logged_in:
                    await self._login_once()
                status, body, _ = await self._send(method, path, json_body=payload, headers=_AJAX_HEADERS)
                try:
                    return parse(self._decode_json(status, body, path))
                except _SessionInvalidError as err:
                    if relogged:
                        raise MainzerNetzeApiError(
                            f"Unexpected response from {path} even after re-login: {err}"
                        ) from err
                    _LOGGER.debug("Session looks invalid (%s), logging in again", err)
                    self._logged_in = False
                    relogged = True

        return await self._with_retries(attempt)

    @staticmethod
    def _decode_json(status: int, body: str, path: str) -> Any:
        if status != 200:
            # e.g. HTTP 311 {"exception": ...} for a missing session (live-verified)
            raise _SessionInvalidError(f"{path} returned HTTP {status}")
        try:
            return json.loads(body)
        except ValueError as err:
            # Typically the HTML login page instead of JSON.
            raise _SessionInvalidError(f"{path} returned non-JSON content") from err

    # --- Authentication ---

    async def async_login(self) -> None:
        """Logs in (with transient-error retries). Raises MainzerNetzeAuthError on bad credentials."""
        await self._with_retries(self._login_once)

    async def _login_once(self) -> None:
        self._logged_in = False
        # Start from a clean cookie jar so a stale JSESSIONID can't interfere.
        self._session.cookie_jar.clear()
        await self._send("GET", PATH_START)

        form = {"login": self._username, "password": self._password}
        status, body, _ = await self._send("POST", PATH_LOGIN_VALIDATE, form=form, headers=_AJAX_HEADERS)
        if status != 200:
            raise _parse_login_error(body)
        try:
            model = json.loads(body).get("model") or {}
        except (ValueError, AttributeError) as err:
            raise MainzerNetzeApiError("Unexpected login validation response") from err
        if isinstance(model, dict) and model.get("captchaRequired"):
            raise MainzerNetzeCaptchaRequiredError(
                "Portal requires a captcha; log in once via the web browser"
            )

        status, body, final_url = await self._send("POST", PATH_LOGIN_PROCESS, form=form)
        if status != 200 or final_url.rstrip("/").endswith(PATH_START) or _LOGIN_FORM_MARKER in body:
            raise MainzerNetzeAuthError("Login rejected by the portal")
        self._logged_in = True
        _LOGGER.debug("Logged in to the Mainzer Netze portal")

    async def async_logout(self) -> None:
        """Ends the portal session. Best-effort, never raises."""
        if not self._logged_in:
            return
        self._logged_in = False
        try:
            await self._send("GET", PATH_LOGOUT, allow_redirects=False)
        except MainzerNetzeError as err:
            _LOGGER.debug("Logout failed (ignored): %s", err)
        self._session.cookie_jar.clear()

    # --- Data ---

    async def async_get_meters(self) -> list[Meter]:
        """Returns all meters visible to the account."""
        return await self._request_json("GET", PATH_TREE, parse_meters)

    async def async_get_meter_details(self, indicator_id: str) -> MeterDetails:
        """Returns features, measured value ranges and last import time of a meter."""
        return await self._request_json(
            "POST",
            PATH_INIT_DATA,
            lambda data: parse_meter_details(data, indicator_id),
            {"currentIndicatorID": indicator_id, "fetchLastImport": True},
        )

    async def async_get_readings(
        self, details: MeterDetails, feature_id: str, start: datetime, end: datetime
    ) -> list[Reading]:
        """Returns quarter-hour readings in [start, end), fetched in chunks."""
        readings: list[Reading] = []
        for chunk_start, chunk_end in split_range(start, end):
            readings.extend(
                await self._request_json(
                    "POST",
                    PATH_VALUES,
                    lambda data: parse_readings(data, details.indicator_id, feature_id),
                    build_value_request(details, feature_id, chunk_start, chunk_end),
                )
            )
        return readings
