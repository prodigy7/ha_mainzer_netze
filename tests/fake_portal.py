"""In-process fake of the Mainzer Netze portal for client tests.

Mimics the live-verified behavior (see PROTOCOL.md), including the quirks:
- missing session: tree -> HTTP 311 {"exception"}, initData -> HTTP 200 {},
  valueRequest -> HTTP 503 HTML
- failed login: loginWidget.json -> HTTP 400 with globalerrors
- loginProcess redirects to /home on success, /start on failure

valueRequest answers with synthetic quarter-hours (0.25 per interval) for
exactly the requested range, so chunking/coverage can be asserted.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from custom_components.mainzer_netze.const import PORTAL_TZ

FIXTURES = Path(__file__).parent / "fixtures"
PREFIX = "/ems/swmn/fo/portal"
USERNAME = "user@example.com"
PASSWORD = "s3cret*%$"
INDICATOR_ID = "00000000-0000-4000-8000-000000000001"
FEATURE_ID = "POWER_ACTIVE_PURCHASE_CONSUMPTION"
QUARTER_HOUR_VALUE = 0.25


def load_fixture(name: str) -> Any:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data.pop("_comment", None)
    return data


def _portal_iso(value: datetime) -> str:
    return value.astimezone(PORTAL_TZ).isoformat(timespec="milliseconds")


def quarter_hours(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Synthetic portal values for [start, end).

    Steps in UTC (absolute time), so the DST fall-back day gets 100
    quarter-hours, rendered with the correct local offsets like the real portal.
    """
    values = []
    cursor = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    while cursor < end_utc:
        nxt = cursor + timedelta(minutes=15)
        values.append(
            {
                "value": QUARTER_HOUR_VALUE,
                "measuringPeriod": {_portal_iso(cursor): _portal_iso(nxt)},
            }
        )
        cursor = nxt
    return values


class FakePortal:
    def __init__(self) -> None:
        self.sessions: dict[str, bool] = {}  # JSESSIONID -> authenticated
        self.captcha_required = False
        self.calls: Counter[str] = Counter()
        self.request_times: list[float] = []
        self.value_ranges: list[tuple[datetime, datetime]] = []
        # path -> list of (status, body, content_type) served before normal handling
        self.forced: dict[str, list[tuple[int, str, str]]] = {}
        self.delay_seconds = 0.0
        self.init_data = load_fixture("init_data.json")
        self.tree = load_fixture("tree.json")

    # --- test controls ---

    def force(self, path: str, status: int, body: str = "", content_type: str = "text/html") -> None:
        self.forced.setdefault(path, []).append((status, body, content_type))

    def expire_all_sessions(self) -> None:
        for key in self.sessions:
            self.sessions[key] = False

    # --- app ---

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get(PREFIX + "/start", self._start)
        app.router.add_get(PREFIX + "/home", self._home)
        app.router.add_post(PREFIX + "/loginWidget.json", self._login_widget)
        app.router.add_post(PREFIX + "/loginProcess", self._login_process)
        app.router.add_get(PREFIX + "/logout", self._logout)
        app.router.add_get(PREFIX + "/indicatorGroupList.json", self._tree)
        app.router.add_post(PREFIX + "/analysis/initData", self._init_data)
        app.router.add_post(PREFIX + "/analysis/valueRequest", self._values)
        return app

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        path = request.path.removeprefix(PREFIX)
        self.calls[path] += 1
        self.request_times.append(asyncio.get_running_loop().time())
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.forced.get(path):
            status, body, content_type = self.forced[path].pop(0)
            return web.Response(status=status, text=body, content_type=content_type)
        return await handler(request)

    def _authenticated(self, request: web.Request) -> bool:
        return self.sessions.get(request.cookies.get("JSESSIONID", ""), False)

    async def _start(self, request: web.Request) -> web.Response:
        token = secrets.token_hex(8)
        self.sessions[token] = False
        resp = web.Response(text='<form id="loginProcessForm"></form>', content_type="text/html")
        resp.set_cookie("JSESSIONID", token, path="/ems/swmn/fo", httponly=True)
        return resp

    async def _home(self, request: web.Request) -> web.Response:
        if not self._authenticated(request):
            raise web.HTTPFound(PREFIX + "/start")
        return web.Response(text="<html>home</html>", content_type="text/html")

    async def _login_widget(self, request: web.Request) -> web.Response:
        form = await request.post()
        model = {"login": form.get("login"), "password": "", "captchaRequired": self.captcha_required}
        if self.captcha_required:
            return web.json_response(
                {"model": model, "hasGlobalErrors": True, "globalerrors": ["Captcha"]}, status=400
            )
        if form.get("login") != USERNAME or form.get("password") != PASSWORD:
            return web.json_response(
                {"model": model, "hasGlobalErrors": True, "globalerrors": ["Login failed"]},
                status=400,
            )
        return web.json_response({"model": model})

    async def _login_process(self, request: web.Request) -> web.Response:
        form = await request.post()
        token = request.cookies.get("JSESSIONID", "")
        if token in self.sessions and form.get("login") == USERNAME and form.get("password") == PASSWORD:
            self.sessions[token] = True
            raise web.HTTPFound(PREFIX + "/home")
        raise web.HTTPFound(PREFIX + "/start")

    async def _logout(self, request: web.Request) -> web.Response:
        self.sessions.pop(request.cookies.get("JSESSIONID", ""), None)
        raise web.HTTPFound(PREFIX + "/start")

    async def _tree(self, request: web.Request) -> web.Response:
        if not self._authenticated(request):
            return web.json_response({"exception": "Fehler"}, status=311)
        return web.json_response(self.tree)

    async def _init_data(self, request: web.Request) -> web.Response:
        body = await request.json()
        if not self._authenticated(request) or body.get("currentIndicatorID") != INDICATOR_ID:
            return web.json_response({})
        return web.json_response(self.init_data)

    async def _values(self, request: web.Request) -> web.Response:
        if not self._authenticated(request):
            return web.Response(status=503, text="<html>Error</html>", content_type="text/html")
        body = await request.json()
        ivr = body["indicatorValueRequests"][0]
        ((start_raw, end_raw),) = ivr["valueRange"].items()
        start, end = datetime.fromisoformat(start_raw), datetime.fromisoformat(end_raw)
        self.value_ranges.append((start, end))
        feature_id = ivr["featureValueRequests"][0]["featureID"]
        return web.json_response(
            {
                "indicatorValueResponses": [
                    {
                        "indicatorID": ivr["indicatorID"],
                        "indicatorType": ivr["indicatorType"],
                        "index": 0,
                        "featureValueResponses": [
                            {
                                "featureID": feature_id,
                                "aggregationType": "SUM",
                                "values": quarter_hours(start, end),
                                "granularityType": "QUARTER_HOUR",
                                "valueRange": ivr["valueRange"],
                            }
                        ],
                    }
                ]
            }
        )
