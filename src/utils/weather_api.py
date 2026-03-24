"""
NOAA/NWS Weather API client — no API key required.
Provides temperature and precipitation forecasts for US cities.

NWS policy requires a descriptive User-Agent; see api.weather.gov/openapi.json.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from src.utils.logger import logger

NWS_BASE = "https://api.weather.gov"
_USER_AGENT = "polymarket-arb-bot/1.0 (github.com/MichaelParizhsky/polymarket-arb-bot)"

# Forecast temperature error std-dev (°F) by lead time.
# Based on NWS public verification statistics.
_TEMP_SIGMA: dict[int, float] = {0: 2.5, 1: 3.5, 2: 4.5, 3: 5.5}

# Major US cities covered by NWS (lat, lon).
CITY_COORDS: dict[str, tuple[float, float]] = {
    "New York":      (40.7128, -74.0060),
    "New York City": (40.7128, -74.0060),
    "NYC":           (40.7128, -74.0060),
    "Chicago":       (41.8781, -87.6298),
    "Miami":         (25.7617, -80.1918),
    "Los Angeles":   (34.0522, -118.2437),
    "Dallas":        (32.7767, -96.7970),
    "Atlanta":       (33.7490, -84.3880),
    "Seattle":       (47.6062, -122.3321),
    "Denver":        (39.7392, -104.9903),
    "Boston":        (42.3601, -71.0589),
    "Houston":       (29.7604, -95.3698),
    "Phoenix":       (33.4484, -112.0740),
    "Philadelphia":  (39.9526, -75.1652),
    "Minneapolis":   (44.9778, -93.2650),
    "Detroit":       (42.3314, -83.0458),
    "Portland":      (45.5051, -122.6750),
    "Las Vegas":     (36.1699, -115.1398),
    "Nashville":     (36.1627, -86.7816),
    "Charlotte":     (35.2271, -80.8431),
    "Kansas City":   (39.0997, -94.5786),
    "San Francisco": (37.7749, -122.4194),
    "San Diego":     (32.7157, -117.1611),
    "Tampa":         (27.9506, -82.4572),
    "Baltimore":     (39.2904, -76.6122),
    "St. Louis":     (38.6270, -90.1994),
    "Pittsburgh":    (40.4406, -79.9959),
    "Cleveland":     (41.4993, -81.6944),
    "Cincinnati":    (39.1031, -84.5120),
    "Indianapolis":  (39.7684, -86.1581),
    "Columbus":      (39.9612, -82.9988),
    "Milwaukee":     (43.0389, -87.9065),
    "Sacramento":    (38.5816, -121.4944),
    "Salt Lake City":(40.7608, -111.8910),
    "New Orleans":   (29.9511, -90.0715),
    "Richmond":      (37.5407, -77.4360),
    "Memphis":       (35.1495, -90.0490),
    "Oklahoma City": (35.4676, -97.5164),
    "Louisville":    (38.2527, -85.7585),
    "Hartford":      (41.7658, -72.6851),
    "Providence":    (41.8240, -71.4128),
    "Albany":        (42.6526, -73.7562),
    "Buffalo":       (42.8864, -78.8784),
    "Raleigh":       (35.7796, -78.6382),
}


@dataclass
class ForecastDay:
    city: str
    target_date: date
    high_f: float | None       # daily high temperature (°F)
    low_f: float | None        # daily low temperature (°F)
    precip_prob: float | None  # 0.0–1.0 probability of any precipitation
    lead_days: int             # days ahead from today (0 = today)
    fetched_at: float = field(default_factory=time.time)


def _normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def prob_above(forecast_temp: float, threshold: float, lead_days: int) -> float:
    """P(actual daily temperature > threshold) given the point forecast."""
    sigma = _TEMP_SIGMA.get(min(lead_days, 3), 5.5)
    z = (threshold - forecast_temp) / sigma
    return 1.0 - _normal_cdf(z)


def prob_below(forecast_temp: float, threshold: float, lead_days: int) -> float:
    """P(actual daily temperature < threshold) given the point forecast."""
    return 1.0 - prob_above(forecast_temp, threshold, lead_days)


class NOAAClient:
    """
    Thin async wrapper around the NWS public forecast API.
    Caches grid metadata (24 h) and forecast periods (30 min).
    """
    _GRID_TTL     = 86400.0
    _FORECAST_TTL = 1800.0

    def __init__(self) -> None:
        self._grid_cache:     dict[str, dict]       = {}
        self._grid_ts:        dict[str, float]      = {}
        self._periods_cache:  dict[str, list[dict]] = {}
        self._periods_ts:     dict[str, float]      = {}

    async def _grid(self, city: str) -> dict | None:
        now = time.time()
        if city in self._grid_cache and now - self._grid_ts.get(city, 0) < self._GRID_TTL:
            return self._grid_cache[city]
        coords = CITY_COORDS.get(city)
        if not coords:
            return None
        lat, lon = coords
        try:
            async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _USER_AGENT}) as c:
                r = await c.get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
                r.raise_for_status()
                p = r.json()["properties"]
                grid = {"office": p["gridId"], "x": p["gridX"], "y": p["gridY"]}
                self._grid_cache[city] = grid
                self._grid_ts[city] = now
                return grid
        except Exception as exc:
            logger.warning(f"[weather] grid lookup failed for {city}: {exc}")
            return None

    async def _periods(self, city: str) -> list[dict]:
        now = time.time()
        if city in self._periods_cache and now - self._periods_ts.get(city, 0) < self._FORECAST_TTL:
            return self._periods_cache[city]
        grid = await self._grid(city)
        if not grid:
            return []
        url = f"{NWS_BASE}/gridpoints/{grid['office']}/{grid['x']},{grid['y']}/forecast"
        try:
            async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": _USER_AGENT}) as c:
                r = await c.get(url)
                r.raise_for_status()
                periods = r.json()["properties"]["periods"]
                self._periods_cache[city] = periods
                self._periods_ts[city] = now
                return periods
        except Exception as exc:
            logger.warning(f"[weather] forecast fetch failed for {city}: {exc}")
            return []

    async def get_forecast(self, city: str, target_date: date) -> ForecastDay | None:
        """Return high temp, low temp, and precip probability for a city on a given date."""
        periods = await self._periods(city)
        if not periods:
            return None

        high_f: float | None = None
        low_f: float | None = None
        precip_prob: float | None = None

        for period in periods:
            try:
                start_dt = datetime.fromisoformat(period["startTime"].replace("Z", "+00:00"))
                if start_dt.date() != target_date:
                    continue
                temp = float(period.get("temperature", 0))
                is_day = period.get("isDaytime", True)
                pp_raw = period.get("probabilityOfPrecipitation", {})
                pp_val = pp_raw.get("value") if isinstance(pp_raw, dict) else None

                if is_day:
                    high_f = temp
                    if pp_val is not None:
                        precip_prob = float(pp_val) / 100.0
                else:
                    low_f = temp
                    if precip_prob is None and pp_val is not None:
                        precip_prob = float(pp_val) / 100.0
            except Exception:
                continue

        if high_f is None and low_f is None:
            return None

        lead_days = max(0, (target_date - date.today()).days)
        return ForecastDay(
            city=city,
            target_date=target_date,
            high_f=high_f,
            low_f=low_f,
            precip_prob=precip_prob,
            lead_days=lead_days,
        )


# Module-level singleton — shared across all strategy instances.
noaa = NOAAClient()
