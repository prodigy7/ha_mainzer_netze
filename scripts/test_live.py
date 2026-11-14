"""Read-only live check of api.py against the real portal (outside of HA).

Usage:  .venv/bin/python scripts/test_live.py [--days N]

Reads credentials from .env (MAINZER_NETZE_USERNAME / MAINZER_NETZE_PASSWORD).
Performs only reads: login, meter list, meter details, readings for the last
N days with data (default 1), logout. About 7 requests in total.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import aiohttp

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _env import load_dotenv  # noqa: E402

from custom_components.mainzer_netze.api import MainzerNetzeClient  # noqa: E402
from custom_components.mainzer_netze.statistics import aggregate_hourly, last_complete_day  # noqa: E402


async def main(days: int) -> None:
    load_dotenv()
    async with aiohttp.ClientSession() as session:
        client = MainzerNetzeClient(
            session, os.environ["MAINZER_NETZE_USERNAME"], os.environ["MAINZER_NETZE_PASSWORD"]
        )
        try:
            meters = await client.async_get_meters()
            print(f"{len(meters)} meter(s)")
            for meter in meters:
                details = await client.async_get_meter_details(meter.indicator_id)
                print(f"- {meter.media_type} meter, last import {details.last_import}")
                for feature in details.features.values():
                    print(
                        f"  {feature.feature_id} [{feature.unit}] "
                        f"{feature.measured_start} .. {feature.measured_end}"
                    )
                    if feature.measured_end is None:
                        continue
                    start = feature.measured_end - timedelta(days=days)
                    readings = await client.async_get_readings(
                        details, feature.feature_id, start, feature.measured_end
                    )
                    hourly = aggregate_hourly(readings)
                    print(
                        f"  {len(readings)} readings -> {len(hourly)} hours, "
                        f"total {sum(r.value for r in readings):.3f}, last day {last_complete_day(readings)}"
                    )
        finally:
            await client.async_logout()
            print("logged out")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    asyncio.run(main(args.days))
