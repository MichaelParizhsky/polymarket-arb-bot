"""
Strategy: Kalshi Weather Markets.

Trades temperature (high/low) and precipitation markets on Kalshi using
NOAA/NWS probabilistic forecasts as the pricing model.

Edge source
-----------
Kalshi weather markets are often priced by retail traders using simple
point forecasts (Weather.com, AccuWeather). NWS ensemble forecasts are
calibrated and more accurate, especially for temperature extremes.

Edge = |NOAA_probability - Kalshi_price| - KALSHI_FEE_RATE

Market types traded
-------------------
  HIGH TEMP  — "Will the high in [city] exceed [X]°F on [date]?"
  LOW TEMP   — "Will the low in [city] fall below [X]°F on [date]?"
  RAIN/PRECIP— "Will it rain in [city] on [date]?"

Configuration (env vars)
------------------------
  STRATEGY_WEATHER=true
  WEATHER_MIN_EDGE=0.05        # net edge required after fees (default 5%)
  WEATHER_MAX_SPEND=75.0       # max USDC per position
  WEATHER_MIN_VOLUME=200.0     # skip illiquid markets below this volume
  WEATHER_MAX_LEAD_DAYS=3      # only trade markets resolving within 3 days
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from typing import Any

from src.exchange.kalshi import KalshiClient, KalshiMarket
from src.strategies.base import BaseStrategy, Signal
from src.utils.logger import logger
from src.utils.weather_api import CITY_COORDS, noaa, prob_above, prob_below

# Kalshi weather markets charge ~3% taker fee (conservative estimate).
KALSHI_FEE_RATE = 0.03

# ── Title parsing ──────────────────────────────────────────────────────────────

# Month name → number
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

_DATE_PATTERNS = [
    re.compile(r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),?\s*(?P<year>\d{4})?"),
]

_HIGH_PATTERN = re.compile(
    r"(?:high|daily high|max(?:imum)?)\s+(?:temperature\s+)?(?:in|for)?\s+"
    r"(?P<city>[A-Za-z\s\.]+?)\s+"
    r"(?:be\s+)?(?:above|exceed[s]?|over|reach[es]?)\s+"
    r"(?P<threshold>-?\d+(?:\.\d+)?)\s*°?[Ff]",
    re.IGNORECASE,
)

_LOW_PATTERN = re.compile(
    r"(?:low|daily low|min(?:imum)?)\s+(?:temperature\s+)?(?:in|for)?\s+"
    r"(?P<city>[A-Za-z\s\.]+?)\s+"
    r"(?:fall|drop|go|be\s+)?(?:below|under)\s+"
    r"(?P<threshold>-?\d+(?:\.\d+)?)\s*°?[Ff]",
    re.IGNORECASE,
)

# Also catch: "high in Chicago above 50°F" without word "temperature"
_HIGH_PATTERN2 = re.compile(
    r"high\s+in\s+(?P<city>[A-Za-z\s\.]+?)\s+"
    r"(?:be\s+)?(?:above|exceed[s]?|over)\s+"
    r"(?P<threshold>-?\d+(?:\.\d+)?)\s*°?[Ff]",
    re.IGNORECASE,
)

_LOW_PATTERN2 = re.compile(
    r"low\s+in\s+(?P<city>[A-Za-z\s\.]+?)\s+"
    r"(?:fall\s+)?(?:below|under)\s+"
    r"(?P<threshold>-?\d+(?:\.\d+)?)\s*°?[Ff]",
    re.IGNORECASE,
)

_RAIN_PATTERN = re.compile(
    r"(?:rain|precipitation|precip|wet)\s+(?:in|for|at)\s+(?P<city>[A-Za-z\s\.]+?)(?:\s+on|\?|$)",
    re.IGNORECASE,
)


def _parse_date(title: str) -> date | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(title)
        if m:
            month_str = m.group("month").lower()
            month = _MONTHS.get(month_str[:3]) or _MONTHS.get(month_str)
            if not month:
                continue
            day = int(m.group("day"))
            year_str = m.group("year") if m.group("year") else None
            year = int(year_str) if year_str else date.today().year
            try:
                d = date(year, month, day)
                # If parsed date is in the past by >1 day, bump to next year
                if (d - date.today()).days < -1:
                    d = date(year + 1, month, day)
                return d
            except ValueError:
                continue
    return None


def _match_city(raw: str) -> str | None:
    """Find the best matching known city name in a raw string."""
    raw_clean = raw.strip().rstrip(" ,?.")
    # Exact match first
    for city in CITY_COORDS:
        if city.lower() == raw_clean.lower():
            return city
    # Prefix match (e.g. "New York City" matches "New York")
    for city in sorted(CITY_COORDS.keys(), key=len, reverse=True):
        if city.lower() in raw_clean.lower() or raw_clean.lower() in city.lower():
            return city
    return None


def _parse_weather_market(market: KalshiMarket) -> dict | None:
    """
    Parse a Kalshi market title into a structured weather event.
    Returns dict with keys: event_type, city, threshold, target_date
    or None if not parseable.
    """
    title = market.title

    # Try high temp patterns
    for pat in (_HIGH_PATTERN, _HIGH_PATTERN2):
        m = pat.search(title)
        if m:
            city = _match_city(m.group("city"))
            target_date = _parse_date(title)
            if city and target_date:
                return {
                    "event_type": "high_temp_above",
                    "city": city,
                    "threshold": float(m.group("threshold")),
                    "target_date": target_date,
                }

    # Try low temp patterns
    for pat in (_LOW_PATTERN, _LOW_PATTERN2):
        m = pat.search(title)
        if m:
            city = _match_city(m.group("city"))
            target_date = _parse_date(title)
            if city and target_date:
                return {
                    "event_type": "low_temp_below",
                    "city": city,
                    "threshold": float(m.group("threshold")),
                    "target_date": target_date,
                }

    # Try precipitation
    m = _RAIN_PATTERN.search(title)
    if m:
        city = _match_city(m.group("city"))
        target_date = _parse_date(title)
        if city and target_date:
            return {
                "event_type": "precip",
                "city": city,
                "threshold": None,
                "target_date": target_date,
            }

    return None


# ── Strategy ───────────────────────────────────────────────────────────────────

class WeatherStrategy(BaseStrategy):
    """
    Trades Kalshi weather markets where NOAA probability diverges from market price.

    Requires KalshiClient. Degrades gracefully if Kalshi is not configured.
    """

    def __init__(self, config, portfolio, risk_manager, kalshi: KalshiClient) -> None:
        super().__init__(config, portfolio, risk_manager)
        self.name = "weather"
        self._kalshi = kalshi
        self._entered: dict[str, float] = {}  # ticker -> entry timestamp

    def _cfg(self):
        return self.config.strategies

    def _cooldown_ok(self, ticker: str) -> bool:
        cooldown_h = getattr(self._cfg(), "weather_cooldown_hours", 6.0)
        last = self._entered.get(ticker, 0)
        return (datetime.utcnow().timestamp() - last) >= cooldown_h * 3600

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        cfg = self._cfg()
        min_edge        = getattr(cfg, "weather_min_edge", 0.05)
        max_spend       = getattr(cfg, "weather_max_spend", 75.0)
        min_volume      = getattr(cfg, "weather_min_volume", 200.0)
        max_lead_days   = getattr(cfg, "weather_max_lead_days", 3)

        signals: list[Signal] = []

        # Fetch all Kalshi markets and filter for weather category
        try:
            all_markets = await self._kalshi.get_markets_cached()
        except Exception as exc:
            self.log(f"Failed to fetch Kalshi markets: {exc}", "warning")
            return []

        weather_markets = [
            m for m in all_markets
            if "weather" in m.category.lower() or "temperature" in m.category.lower()
            or "weather" in m.title.lower() or "temperature" in m.title.lower()
            or "precip" in m.title.lower() or "rainfall" in m.title.lower()
        ]

        if not weather_markets:
            self.log("No weather markets found on Kalshi", "debug")
            return []

        self.log(f"Scanning {len(weather_markets)} weather markets")

        # Parse and evaluate each market concurrently
        tasks = [self._evaluate(m, min_edge, max_spend, min_volume, max_lead_days)
                 for m in weather_markets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Signal):
                signals.append(result)
            elif isinstance(result, Exception):
                self.log(f"Evaluation error: {result}", "debug")

        if signals:
            self.log(f"Generated {len(signals)} weather signal(s)")
        return signals

    async def _evaluate(
        self,
        market: KalshiMarket,
        min_edge: float,
        max_spend: float,
        min_volume: float,
        max_lead_days: int,
    ) -> Signal | None:
        # Volume filter
        if market.volume < min_volume:
            return None

        # Cooldown
        if not self._cooldown_ok(market.ticker):
            return None

        # Parse title
        parsed = _parse_weather_market(market)
        if not parsed:
            self.log(f"Could not parse: {market.title!r}", "debug")
            return None

        target_date = parsed["target_date"]
        today = date.today()
        lead_days = (target_date - today).days

        # Only trade near-term markets within lead time limit
        if lead_days < 0 or lead_days > max_lead_days:
            return None

        # Get NOAA forecast
        forecast = await noaa.get_forecast(parsed["city"], target_date)
        if not forecast:
            self.log(f"No NOAA forecast for {parsed['city']} on {target_date}", "debug")
            return None

        # Calculate model probability
        event_type = parsed["event_type"]
        threshold = parsed["threshold"]
        model_prob: float | None = None

        if event_type == "high_temp_above":
            if forecast.high_f is None:
                return None
            model_prob = prob_above(forecast.high_f, threshold, lead_days)

        elif event_type == "low_temp_below":
            if forecast.low_f is None:
                return None
            model_prob = prob_below(forecast.low_f, threshold, lead_days)

        elif event_type == "precip":
            if forecast.precip_prob is None:
                return None
            model_prob = forecast.precip_prob

        if model_prob is None:
            return None

        # Clamp to reasonable range (avoid extreme overconfidence)
        model_prob = max(0.02, min(0.98, model_prob))

        # Compare to market price
        yes_ask = market.yes_ask   # price to buy YES
        yes_bid = market.yes_bid   # price to sell YES

        # Case A: model says YES is more likely than market prices
        buy_yes_edge = model_prob - yes_ask - KALSHI_FEE_RATE
        # Case B: model says NO is more likely than market prices
        buy_no_edge = (1.0 - model_prob) - (1.0 - yes_bid) - KALSHI_FEE_RATE

        if buy_yes_edge >= min_edge:
            side, edge, price = "BUY", buy_yes_edge, yes_ask
        elif buy_no_edge >= min_edge:
            side, edge, price = "BUY_NO", buy_no_edge, 1.0 - yes_bid
        else:
            return None

        # Skip if price is stale / no liquidity
        if yes_ask <= 0 or yes_bid <= 0 or yes_ask > 1 or yes_bid > 1:
            return None

        size = self.risk.size_position(edge, max_spend)
        if size < 1.0:
            return None

        event_desc = (
            f"{event_type.replace('_', ' ')} {threshold}°F"
            if threshold else event_type
        )
        notes = (
            f"NOAA {parsed['city']} {target_date}: model={model_prob:.1%} "
            f"market={'YES' if side=='BUY' else 'NO'}@{price:.2f} edge={edge:.1%} "
            f"(high={forecast.high_f}°F low={forecast.low_f}°F precip={forecast.precip_prob})"
        )

        self.log(
            f"Signal: {side} {market.ticker} | {event_desc} on {target_date} | "
            f"model={model_prob:.1%} price={price:.2f} edge={edge:.1%} size=${size:.0f}"
        )

        # Use ticker as token_id; metadata flags this as a Kalshi market
        return Signal(
            strategy=self.name,
            token_id=market.ticker,
            side="BUY",
            price=price,
            size_usdc=size,
            edge=edge,
            notes=notes,
            metadata={
                "exchange": "kalshi",
                "ticker": market.ticker,
                "buy_side": side,       # BUY=YES contract, BUY_NO=NO contract
                "city": parsed["city"],
                "event_type": event_type,
                "threshold": threshold,
                "target_date": str(target_date),
                "model_prob": round(model_prob, 4),
                "market_price": round(price, 4),
                "lead_days": lead_days,
            },
        )

    def record_entry(self, ticker: str) -> None:
        self._entered[ticker] = datetime.utcnow().timestamp()
