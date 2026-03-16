"""
Kalshi exchange client.
Fetches markets and orderbooks from the Kalshi trading API.
Paper trading mode simulates order execution without touching real capital.
Gracefully degrades to empty data when no credentials are configured.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.utils.logger import logger
from src.utils.metrics import api_latency


# ------------------------------------------------------------------ #
#  Data types                                                          #
# ------------------------------------------------------------------ #

@dataclass
class KalshiMarket:
    ticker: str           # e.g. "KXBTCD-25DEC31-T100000"
    title: str            # human-readable question
    yes_bid: float        # 0-1 range
    yes_ask: float        # 0-1 range
    no_bid: float         # 0-1 range
    no_ask: float         # 0-1 range
    volume: float
    open_interest: float
    expiry_time: str      # ISO format
    category: str
    status: str


@dataclass
class KalshiOrderbook:
    ticker: str
    yes_bids: list[tuple[float, int]]   # (price 0-1, size)
    yes_asks: list[tuple[float, int]]   # (price 0-1, size)
    timestamp: float = field(default_factory=time.time)

    @property
    def best_yes_bid(self) -> float | None:
        """Highest resting bid price on the YES side."""
        return self.yes_bids[0][0] if self.yes_bids else None

    @property
    def best_yes_ask(self) -> float | None:
        """Lowest resting ask price on the YES side."""
        return self.yes_asks[0][0] if self.yes_asks else None

    @property
    def mid(self) -> float | None:
        """Mid-price between best bid and best ask."""
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return None


# ------------------------------------------------------------------ #
#  Client                                                              #
# ------------------------------------------------------------------ #

class KalshiClient:
    """
    Async Kalshi trading API client.

    Authentication priority:
      1. KALSHI_API_TOKEN env var  -> Bearer token used directly.
      2. KALSHI_EMAIL + KALSHI_PASSWORD env vars -> POST /login to obtain token.
      3. Neither present -> degraded mode (returns empty data, warns once).

    Use as an async context manager::

        async with KalshiClient(config, paper_trading=True) as client:
            markets = await client.get_markets_cached()
    """

    API_BASE = "https://trading-api.kalshi.com/trade-api/v2"
    _CACHE_TTL = 30.0  # seconds

    def __init__(self, config, paper_trading: bool = True) -> None:
        self.config = config
        self.paper_trading = paper_trading

        self._token: str | None = os.getenv("KALSHI_API_TOKEN")
        self._email: str | None = os.getenv("KALSHI_EMAIL")
        self._password: str | None = os.getenv("KALSHI_PASSWORD")

        self._http: httpx.AsyncClient | None = None
        self._no_creds_warned: bool = False

        # Market cache
        self._market_cache: list[KalshiMarket] = []
        self._cache_ts: float = 0.0

    # ------------------------------------------------------------------ #
    #  Context manager                                                     #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "KalshiClient":
        self._http = httpx.AsyncClient(
            base_url=self.API_BASE,
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": "polymarket-arb-bot/1.0"},
        )
        # Try to acquire a token if we only have email+password.
        if not self._token and self._email and self._password:
            await self._login()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()

    # ------------------------------------------------------------------ #
    #  Auth helpers                                                        #
    # ------------------------------------------------------------------ #

    async def _login(self) -> None:
        """POST /login to exchange email+password for a session token."""
        try:
            resp = await self._http.post(
                "/login",
                json={"email": self._email, "password": self._password},
            )
            resp.raise_for_status()
            self._token = resp.json().get("token")
            if self._token:
                logger.info("KalshiClient: authenticated via email/password login")
            else:
                logger.warning("KalshiClient: /login succeeded but no token in response")
        except Exception as exc:
            logger.warning(f"KalshiClient: login failed ({exc}); will operate in degraded mode")

    def _auth_headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _has_credentials(self) -> bool:
        return bool(self._token)

    def _warn_no_creds(self) -> None:
        if not self._no_creds_warned:
            logger.warning(
                "KalshiClient: no API credentials found "
                "(set KALSHI_API_TOKEN or KALSHI_EMAIL+KALSHI_PASSWORD). "
                "Returning empty data."
            )
            self._no_creds_warned = True

    # ------------------------------------------------------------------ #
    #  Market data                                                         #
    # ------------------------------------------------------------------ #

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    async def get_markets(
        self,
        limit: int = 100,
        status: str = "open",
    ) -> list[KalshiMarket]:
        """
        Fetch active markets from Kalshi.

        Returns an empty list (with a one-time warning) if credentials are
        absent, so the rest of the bot can continue without Kalshi data.
        """
        if not self._has_credentials():
            self._warn_no_creds()
            return []

        start = time.perf_counter()
        try:
            resp = await self._http.get(
                "/markets",
                params={"limit": limit, "status": status},
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                logger.warning(f"KalshiClient: auth error fetching markets ({exc}); returning []")
                return []
            raise
        finally:
            elapsed = time.perf_counter() - start
            api_latency.labels(endpoint="kalshi_get_markets").observe(elapsed)

        markets: list[KalshiMarket] = []
        for raw in resp.json().get("markets", []):
            try:
                markets.append(_parse_market(raw))
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug(f"KalshiClient: skipping malformed market entry: {exc}")

        logger.debug(f"KalshiClient: fetched {len(markets)} markets")
        return markets

    async def get_markets_cached(self) -> list[KalshiMarket]:
        """Return markets from cache, refreshing if the cache is stale (30 s TTL)."""
        now = time.time()
        if now - self._cache_ts > self._CACHE_TTL or not self._market_cache:
            self._market_cache = await self.get_markets(limit=500)
            self._cache_ts = now
            logger.debug(
                f"KalshiClient: refreshed market cache ({len(self._market_cache)} markets)"
            )
        return list(self._market_cache)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=0.5, max=3))
    async def get_orderbook(self, ticker: str) -> KalshiOrderbook | None:
        """
        Fetch the YES/NO orderbook for *ticker*.

        Returns None if credentials are absent or the request fails gracefully.
        Kalshi prices arrive in cents (0-99); they are divided by 100 before
        being stored so all downstream code works in the 0-1 range.
        """
        if not self._has_credentials():
            self._warn_no_creds()
            return None

        start = time.perf_counter()
        try:
            resp = await self._http.get(
                f"/markets/{ticker}/orderbook",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.debug(f"KalshiClient: orderbook not found for ticker={ticker}")
                return None
            if exc.response.status_code in (401, 403):
                logger.warning(f"KalshiClient: auth error fetching orderbook ({exc})")
                return None
            raise
        finally:
            elapsed = time.perf_counter() - start
            api_latency.labels(endpoint="kalshi_get_orderbook").observe(elapsed)

        data = resp.json().get("orderbook", resp.json())
        return _parse_orderbook(ticker, data)


# ------------------------------------------------------------------ #
#  Parsing helpers                                                     #
# ------------------------------------------------------------------ #

def _parse_market(raw: dict) -> KalshiMarket:
    """
    Convert a raw Kalshi market dict into a KalshiMarket dataclass.
    Prices from the API are in cents; divide by 100 to get the 0-1 range.
    """
    result = raw.get("result", raw)  # some endpoints nest under "result"

    def _cents(val) -> float:
        return float(val or 0) / 100.0

    return KalshiMarket(
        ticker=str(result.get("ticker", "")),
        title=str(result.get("title", "")),
        yes_bid=_cents(result.get("yes_bid")),
        yes_ask=_cents(result.get("yes_ask")),
        no_bid=_cents(result.get("no_bid")),
        no_ask=_cents(result.get("no_ask")),
        volume=float(result.get("volume", 0)),
        open_interest=float(result.get("open_interest", 0)),
        expiry_time=str(result.get("expiration_time") or result.get("close_time", "")),
        category=str(result.get("category", "")),
        status=str(result.get("status", "")),
    )


def _parse_orderbook(ticker: str, data: dict) -> KalshiOrderbook:
    """
    Build a KalshiOrderbook from raw API data.

    The Kalshi orderbook payload looks like::

        {
          "yes": [[price_cents, size], ...],   # sorted descending
          "no":  [[price_cents, size], ...]    # sorted descending
        }

    We expose only the YES side (bids and asks) in the 0-1 range.
    The YES ask prices are derived from the NO bids: yes_ask = 1 - no_bid.
    """
    def _to_01(price_cents: int | float) -> float:
        return float(price_cents) / 100.0

    raw_yes: list[list] = data.get("yes", [])
    raw_no: list[list] = data.get("no", [])

    # YES bids: resting buy orders on YES, sorted best (highest) first.
    yes_bids: list[tuple[float, int]] = sorted(
        [(_to_01(row[0]), int(row[1])) for row in raw_yes if len(row) >= 2],
        key=lambda x: x[0],
        reverse=True,
    )

    # YES asks are implicitly the complement of NO bids:
    # if someone bids 55 cents on NO, the implied YES ask is 45 cents.
    yes_asks: list[tuple[float, int]] = sorted(
        [(round(1.0 - _to_01(row[0]), 6), int(row[1])) for row in raw_no if len(row) >= 2],
        key=lambda x: x[0],
    )

    return KalshiOrderbook(
        ticker=ticker,
        yes_bids=yes_bids,
        yes_asks=yes_asks,
        timestamp=time.time(),
    )
