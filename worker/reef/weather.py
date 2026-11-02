"""The outdoor temperature beside the tank's, from Open-Meteo (free, no key; https://open-meteo.com). Off until the
site has a latitude and longitude (site.yaml weather). Hourly values go into the history as the external sensor of
site.yaml (charts and the weekly report only: no alerts, no analysis). The first time, the archive fills the same
weeks the tank's history holds.

Each request sends the location rounded to two decimals (about a kilometre) and nothing else."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
SERIAL = "open-meteo"


def parse_hourly(doc: Any) -> list[tuple[float, float]]:
    """{"hourly": {"time": ["2026-10-13T05:00", ...] (UTC, timezone=GMT), "temperature_2m": [51.2, ...]}} ->
    [(ts, temperature)], skipping hours without a value."""
    hourly = doc.get("hourly") if isinstance(doc, dict) else None
    if not isinstance(hourly, dict):
        raise ValueError("no hourly values in the answer")
    out = []
    for when, value in zip(hourly.get("time") or [], hourly.get("temperature_2m") or [], strict=False):
        if value is None:
            continue
        ts = datetime.fromisoformat(when).replace(tzinfo=UTC).timestamp()
        out.append((ts, float(value)))
    return out


async def fetch(
    client: httpx.AsyncClient, latitude: float, longitude: float, now: float, backfill_days: int = 0, celsius: bool = False
) -> list[tuple[float, float]]:
    """The hourly outdoor temperature up to now (°F, or °C): the last two days, or (backfill_days) the archive's days
    before."""
    common = {"latitude": f"{latitude:.2f}", "longitude": f"{longitude:.2f}", "hourly": "temperature_2m",
              "temperature_unit": "celsius" if celsius else "fahrenheit", "timezone": "GMT"}  # fmt: skip
    if backfill_days:
        today = datetime.fromtimestamp(now, UTC).date()
        params = common | {"start_date": (today - timedelta(days=backfill_days)).isoformat(),
                           "end_date": (today - timedelta(days=2)).isoformat()}  # fmt: skip
        response = await client.get(ARCHIVE, params=params)
    else:
        response = await client.get(FORECAST, params=common | {"past_days": 2, "forecast_days": 1})
    response.raise_for_status()
    return [(ts, v) for ts, v in parse_hourly(response.json()) if ts <= now]
