"""
Polymarket CLOB client wrapper.
Handles market data, orderbook queries, and order placement.
Paper trading mode skips actual order placement.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.utils.logger import logger
from src.utils.metrics import api_latency


@dataclass(slots=True)
class Token:
    token_id: str
    outcome: str  # "Yes" or "No"


@dataclass
class Market:
    condition_id: str
    question: str
    tokens: list[Token]
    active: bool
    closed: bool
    end_date_iso: str
    tags: list[str] = field(default_factory=list)
    volume: float = 0.0
    liquidity: float = 0.0
    category: str = ""  # e.g. "sports", "crypto", "politics"


@dataclass(slots=True)
class OrderbookLevel:
    price: float
    size: float


@dataclass
class Orderbook:
    token_id: str
    bids: list[OrderbookLevel]  # sorted descending
    asks: list[OrderbookLevel]  # sorted ascending
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class OrderResult:
    order_id: str
    status: str  # "LIVE", "MATCHED", "FILLED", "CANCELLED"
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    token_id: str
    filled_size: float = 0.0
    avg_fill_price: float = 0.0


class PolymarketClient:
    """
    Async Polymarket CLOB client.
    In paper mode, order placement is simulated locally.
    """

    CLOB_BASE = "https://clob.polymarket.com"
    GAMMA_BASE = "https://gamma-api.polymarket.com"

    SPORTS_SERIES_IDS = {
        # North American leagues
        "nfl":  10187,
        "cfb":  10210,
        "nba":  10345,
        "cbb":  10470,
        "nhl":  10346,
        "mls":  10189,
        # Soccer / football
        "bundesliga":         10194,
        "norway_eliteserien": 10362,
        "brazil_serie_a":     10359,
        "japan_j_league":     10360,
    }

    def __init__(self, config, paper_trading: bool = True) -> None:
        self.config = config
        self.paper_trading = paper_trading
        self._http: httpx.AsyncClient | None = None
        self._market_cache: dict[str, Market] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 30.0  # seconds
        self._clob_client = None  # initialised once in __aenter__ for live mode
        self._expiring_cache: list = []
        self._expiring_cache_ts: float = 0.0

    async def __aenter__(self) -> "PolymarketClient":
        # Connection pooling: reuse connections (avoids TLS handshake on every request)
        # limits.max_connections=100 allows burst; keepalive_expiry=30s recycles idle connections
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            headers={"User-Agent": "polymarket-arb-bot/1.0"},
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
        )
        # Pre-build the ClobClient once for the session to avoid per-order overhead
        self._clob_is_sync = False
        if not self.paper_trading and self.config.private_key:
            try:
                from py_clob_client.constants import POLYGON
                from py_clob_client.clob_types import ApiCreds
                _creds = ApiCreds(
                    api_key=self.config.api_key,
                    api_secret=self.config.api_secret,
                    api_passphrase=self.config.api_passphrase,
                )
                try:
                    from py_clob_client.client import AsyncClobClient
                    self._clob_client = AsyncClobClient(
                        host=self.CLOB_BASE,
                        chain_id=POLYGON,
                        key=self.config.private_key,
                        creds=_creds,
                        signature_type=2,
                        funder=self.config.funder_address,
                    )
                    logger.info("AsyncClobClient initialised (session-level, gasless via CLOB relayer)")
                except ImportError:
                    from py_clob_client.client import ClobClient
                    self._clob_client = ClobClient(
                        host=self.CLOB_BASE,
                        chain_id=POLYGON,
                        key=self.config.private_key,
                        creds=_creds,
                        signature_type=2,
                        funder=self.config.funder_address,
                    )
                    self._clob_is_sync = True
                    logger.info("ClobClient initialised (sync fallback, run_in_executor)")
            except Exception as exc:
                logger.warning(f"ClobClient init failed: {exc}")
                self._clob_client = None
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()

    # ------------------------------------------------------------------ #
    #  Market data                                                          #
    # ------------------------------------------------------------------ #

    def _parse_market(self, raw: dict) -> Market | None:
        """
        Convert a raw Gamma API market dict into a Market dataclass.
        Returns None if the market is malformed or has fewer than 2 tokens.
        """
        try:
            token_ids = raw.get("clobTokenIds") or []
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            outcomes = raw.get("outcomes") or ["Yes", "No"]
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            tokens = [
                Token(token_id=str(tid), outcome=str(out))
                for tid, out in zip(token_ids, outcomes)
            ]
            if len(tokens) < 2:
                return None

            condition_id = raw.get("conditionId") or raw.get("id") or ""
            cat = raw.get("category") or ""
            return Market(
                condition_id=str(condition_id),
                question=raw.get("question", ""),
                tokens=tokens,
                active=raw.get("active", False),
                closed=raw.get("closed", True),
                end_date_iso=raw.get("endDate") or raw.get("endDateIso", ""),
                tags=[cat] if cat else [],
                volume=float(raw.get("volumeNum") or raw.get("volume") or 0),
                liquidity=float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
                category=cat.lower(),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    async def get_markets(self, limit: int = 100, offset: int = 0) -> list[Market]:
        """Fetch active markets from Gamma API."""
        start = time.perf_counter()
        resp = await self._http.get(
            f"{self.GAMMA_BASE}/markets",
            params={
                "limit": limit,
                "offset": offset,
                "active": "true",
                "closed": "false",
            },
        )
        resp.raise_for_status()
        elapsed = time.perf_counter() - start
        api_latency.labels(endpoint="get_markets").observe(elapsed)

        markets = []
        for raw in resp.json():
            market = self._parse_market(raw)
            if market:
                markets.append(market)
            else:
                logger.debug(f"Skipping malformed market: {raw.get('id') or raw.get('conditionId')}")
        return markets

    async def get_market_by_condition_id(self, condition_id: str) -> dict | None:
        """Fetch raw market data from Gamma API by condition ID.
        Returns the raw dict (includes 'closed', 'active' resolution fields).
        Returns None on failure.
        """
        try:
            resp = await self._http.get(
                f"{self.GAMMA_BASE}/markets",
                params={"condition_id": condition_id},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            return None
        except Exception as exc:
            logger.debug(f"get_market_by_condition_id({condition_id[:16]}): {exc}")
            return None

    async def get_markets_cached(self) -> list[Market]:
        """Return cached markets, refreshing if stale."""
        now = time.time()
        if now - self._cache_ts > self._cache_ttl or not self._market_cache:
            markets = await self.get_markets(limit=500)
            self._market_cache = {m.condition_id: m for m in markets}
            self._cache_ts = now
            logger.debug(f"Refreshed market cache: {len(self._market_cache)} markets")
        return list(self._market_cache.values())

    async def get_expiring_markets(self, max_hours: float = 48.0) -> list[Market]:
        """
        Fetch markets expiring within the next `max_hours` hours, sorted by end_date ascending.
        Supplements get_markets() so near-expiry markets are never missed due to pagination.
        Uses API-side date filtering to reduce payload size; client-side filter is kept as a
        safety fallback.
        """
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        end_max = now + _dt.timedelta(hours=max_hours)

        params = {
            "active": "true",
            "closed": "false",
            "limit": 500,
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": end_max.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        try:
            resp = await self._http.get(
                f"{self.GAMMA_BASE}/markets",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(f"get_expiring_markets error: {exc}")
            return []

        markets_raw = data if isinstance(data, list) else data.get("data", [])
        markets = []
        for raw in markets_raw:
            try:
                market = self._parse_market(raw)
                if not market:
                    continue

                end_date_iso = market.end_date_iso
                if not end_date_iso:
                    continue

                # Client-side safety filter
                try:
                    s = end_date_iso.strip().rstrip("Z")
                    if "T" not in s:
                        s += "T00:00:00"
                    end_dt = _dt.datetime.fromisoformat(s).replace(
                        tzinfo=_dt.timezone.utc
                    )
                    hours_left = (end_dt - now).total_seconds() / 3600
                    if not (0 < hours_left <= max_hours):
                        continue
                except (ValueError, TypeError):
                    continue

                markets.append(market)
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug(f"get_expiring_markets: skipping malformed market: {exc}")

        logger.info(f"get_expiring_markets: found {len(markets)} markets expiring within {max_hours}h")
        return markets

    async def _fetch_markets_by_slug(self, slug: str) -> list[Market]:
        """Single Gamma slug lookup; used in parallel by get_crypto_short_markets."""
        out: list[Market] = []
        if not self._http:
            return out
        try:
            resp = await self._http.get(
                f"{self.GAMMA_BASE}/markets/slug/{slug}",
                timeout=httpx.Timeout(10.0),
            )
            if resp.status_code != 200:
                return out
            data = resp.json()
            items = data if isinstance(data, list) else [data]
            for raw in items:
                try:
                    market = self._parse_market(raw)
                    if market:
                        out.append(market)
                except Exception as exc:
                    logger.debug(f"crypto_short: parse error {slug}: {exc}")
        except Exception as exc:
            logger.debug(f"crypto_short: fetch error {slug}: {exc}")
        return out

    async def _fetch_sports_series_raw(
        self,
        sport: str,
        series_id: int,
        end_min: str,
        end_max: str,
    ) -> tuple[str, Any]:
        """One series_id /events request; results merged in get_sports_markets."""
        if not self._http:
            return sport, None
        try:
            resp = await self._http.get(
                f"{self.GAMMA_BASE}/events",
                params={
                    "series_id": series_id,
                    "active": "true",
                    "closed": "false",
                    "limit": 200,
                    "end_date_min": end_min,
                    "end_date_max": end_max,
                },
                timeout=httpx.Timeout(10.0),
            )
            if resp.status_code != 200:
                return sport, None
            return sport, resp.json()
        except Exception as exc:
            logger.warning(f"sports_markets: fetch error {sport}: {exc}")
            return sport, None

    async def get_expiring_markets_cached(self, max_hours: float = 48.0) -> list[Market]:
        """Cached version of get_expiring_markets with 60s TTL."""
        if time.time() - self._expiring_cache_ts < 60.0 and self._expiring_cache:
            return self._expiring_cache
        markets = await self.get_expiring_markets(max_hours=max_hours)
        self._expiring_cache = markets
        self._expiring_cache_ts = time.time()
        return markets

    async def get_crypto_short_markets(
        self,
        coins: list[str] | None = None,
        include_next_window: bool = True,
    ) -> list[Market]:
        """
        Discover current 5-minute and 15-minute crypto up/down markets using
        deterministic slug calculation. These markets rotate on a fixed schedule
        (5m = every 300s, 15m = every 900s) so we compute the slug from the clock
        instead of searching the API.

        Working slug format:
            {coin}-updown-{duration}-{window_start_unix}
        Examples:
            btc-updown-5m-1768502700
            eth-updown-15m-1768824000
        """
        if coins is None:
            coins = ["btc", "eth", "sol", "xrp"]

        now = int(time.time())
        slugs: list[str] = []
        for coin in coins:
            for duration, window in [("5m", 300), ("15m", 900)]:
                ts = (now // window) * window
                slugs.append(f"{coin}-updown-{duration}-{ts}")
                if include_next_window:
                    slugs.append(f"{coin}-updown-{duration}-{ts + window}")

        # Parallel slug fetches on pooled HTTP — sequential loop + new client per run was
        # multi-second latency; this keeps crypto windows competitive vs other bots.
        if not self._http:
            logger.warning("get_crypto_short_markets: HTTP client not initialised")
            return []
        results = await asyncio.gather(
            *[self._fetch_markets_by_slug(s) for s in slugs],
            return_exceptions=True,
        )
        seen: set[str] = set()
        markets: list[Market] = []
        for res in results:
            if isinstance(res, Exception):
                continue
            for m in res:
                if m.condition_id not in seen:
                    seen.add(m.condition_id)
                    markets.append(m)

        logger.info(f"crypto_short_markets: found {len(markets)} active crypto short markets")
        return markets

    async def get_sports_markets(
        self,
        max_hours: float = 48.0,
    ) -> list[Market]:
        """
        Fetch active sports markets using Polymarket's series_id parameter.
        This is the only reliable way to find sports markets — keyword matching
        on question text misses many markets.
        """
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        markets: list[Market] = []
        seen: set[str] = set()

        end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_max = (now + _dt.timedelta(hours=max_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

        sports_items = list(self.SPORTS_SERIES_IDS.items())
        raw_results = await asyncio.gather(
            *[
                self._fetch_sports_series_raw(sp, sid, end_min, end_max)
                for sp, sid in sports_items
            ],
            return_exceptions=True,
        )
        for item in raw_results:
            if isinstance(item, Exception):
                logger.warning(f"sports_markets: fetch error: {item}")
                continue
            sport, data = item
            if data is None:
                continue
            events = data if isinstance(data, list) else data.get("data", [])
            for event in events:
                raw_markets = event.get("markets", [event]) if "markets" in event else [event]
                for raw in raw_markets:
                    try:
                        market = self._parse_market(raw)
                        if market and market.condition_id not in seen:
                            s = market.end_date_iso.strip().rstrip("Z")
                            if "T" not in s:
                                s += "T00:00:00"
                            end_dt = _dt.datetime.fromisoformat(s).replace(
                                tzinfo=_dt.timezone.utc
                            )
                            hours_left = (end_dt - now).total_seconds() / 3600
                            if 0 < hours_left <= max_hours:
                                market.category = sport
                                markets.append(market)
                                seen.add(market.condition_id)
                    except Exception as exc:
                        logger.debug(f"sports_markets: parse error {sport}: {exc}")

        logger.info(f"sports_markets: found {len(markets)} active sports markets within {max_hours}h")
        return markets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=0.5, max=3))
    async def get_orderbook(self, token_id: str) -> Orderbook:
        """Fetch live orderbook for a token."""
        start = time.perf_counter()
        resp = await self._http.get(
            f"{self.CLOB_BASE}/book",
            params={"token_id": token_id},
        )
        resp.raise_for_status()
        elapsed = time.perf_counter() - start
        api_latency.labels(endpoint="get_orderbook").observe(elapsed)

        data = resp.json()
        bids = sorted(
            [OrderbookLevel(price=float(b["price"]), size=float(b["size"])) for b in data.get("bids", [])],
            key=lambda x: x.price, reverse=True
        )
        asks = sorted(
            [OrderbookLevel(price=float(a["price"]), size=float(a["size"])) for a in data.get("asks", [])],
            key=lambda x: x.price
        )
        return Orderbook(token_id=token_id, bids=bids, asks=asks)

    async def get_last_trade_price(self, token_id: str) -> float | None:
        """Get most recent trade price for a token."""
        try:
            resp = await self._http.get(
                f"{self.CLOB_BASE}/last-trade-price",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("price", 0)) if data.get("price") else None
        except Exception as exc:
            logger.warning(f"Failed to get last trade price for {token_id}: {exc}")
            return None

    async def get_midpoint_prices(self, token_ids: list[str]) -> dict[str, float]:
        """Batch fetch midpoint prices."""
        results = {}
        tasks = [self.get_orderbook(tid) for tid in token_ids]
        books = await asyncio.gather(*tasks, return_exceptions=True)
        for tid, book in zip(token_ids, books):
            if isinstance(book, Exception):
                logger.debug(f"Failed orderbook for {tid}: {book}")
                continue
            if book.mid is not None:
                results[tid] = book.mid
        return results

    # ------------------------------------------------------------------ #
    #  Order management                                                     #
    # ------------------------------------------------------------------ #

    async def place_market_order(
        self,
        token_id: str,
        side: str,
        amount_usdc: float,
    ) -> OrderResult | None:
        """
        Place a market order.
        In paper mode, simulates fill at current best price.
        """
        if self.paper_trading:
            return await self._simulate_market_order(token_id, side, amount_usdc)

        return await self._live_market_order(token_id, side, amount_usdc)

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> OrderResult | None:
        """Place a limit order (GTC)."""
        if self.paper_trading:
            return await self._simulate_limit_order(token_id, side, price, size)

        return await self._live_limit_order(token_id, side, price, size)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self.paper_trading:
            logger.info(f"[PAPER] Cancel order {order_id}")
            return True

        try:
            headers = self._auth_headers("DELETE", f"/order/{order_id}")
            resp = await self._http.delete(
                f"{self.CLOB_BASE}/order/{order_id}",
                headers=headers,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Failed to cancel order {order_id}: {exc}")
            return False

    # ------------------------------------------------------------------ #
    #  Paper trading simulation                                             #
    # ------------------------------------------------------------------ #

    async def _simulate_market_order(
        self, token_id: str, side: str, amount_usdc: float
    ) -> OrderResult:
        """Simulate a market order fill at best available price."""
        try:
            book = await self.get_orderbook(token_id)
            if side == "BUY":
                fill_price = book.best_ask or 0.5
            else:
                fill_price = book.best_bid or 0.5
        except Exception:
            fill_price = 0.5

        # Apply small simulated slippage
        slippage = 0.002
        if side == "BUY":
            fill_price = min(fill_price * (1 + slippage), 0.99)
        else:
            fill_price = max(fill_price * (1 - slippage), 0.01)

        contracts = amount_usdc / fill_price if fill_price > 0 else 0
        order_id = f"paper_{token_id[:8]}_{int(time.time()*1000)}"
        logger.info(
            f"[PAPER] MARKET {side} {contracts:.2f} contracts @ {fill_price:.4f} "
            f"(${amount_usdc:.2f}) token={token_id[:16]}..."
        )
        return OrderResult(
            order_id=order_id,
            status="FILLED",
            price=fill_price,
            size=contracts,
            side=side,
            token_id=token_id,
            filled_size=contracts,
            avg_fill_price=fill_price,
        )

    async def _simulate_limit_order(
        self, token_id: str, side: str, price: float, size: float
    ) -> OrderResult:
        """Simulate a limit order placement, filling immediately if price crosses best quote."""
        order_id = f"paper_lim_{token_id[:8]}_{int(time.time()*1000)}"
        fill_status = "LIVE"
        fill_price = price
        filled_size = 0.0

        try:
            book = await self.get_orderbook(token_id)
            if side.upper() == "BUY" and book.best_ask is not None and price >= book.best_ask:
                fill_price = book.best_ask
                fill_status = "FILLED"
                filled_size = size
            elif side.upper() == "SELL" and book.best_bid is not None and price <= book.best_bid:
                fill_price = book.best_bid
                fill_status = "FILLED"
                filled_size = size
        except Exception:
            pass  # fallback: order stays LIVE

        logger.info(
            f"[PAPER] LIMIT {side} {size:.2f} @ {price:.4f} → {fill_status} "
            f"(fill_price={fill_price:.4f})"
        )
        return OrderResult(
            order_id=order_id,
            status=fill_status,
            price=fill_price,
            size=size,
            side=side,
            token_id=token_id,
            filled_size=filled_size,
            avg_fill_price=fill_price if fill_status == "FILLED" else 0.0,
        )

    # ------------------------------------------------------------------ #
    #  Live order helpers (require auth)                                    #
    # ------------------------------------------------------------------ #

    async def _live_market_order(
        self, token_id: str, side: str, amount_usdc: float
    ) -> OrderResult | None:
        """Execute a real market order via CLOB API (FOK — full fill or cancel)."""
        if not self._clob_client:
            logger.error("ClobClient not initialised — cannot place live order")
            return None
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usdc,
                side=side.upper(),
            )
            try:
                loop = asyncio.get_event_loop()
                if self._clob_is_sync:
                    import functools
                    signed = await asyncio.wait_for(
                        loop.run_in_executor(None, self._clob_client.create_market_order, order_args),
                        timeout=5.0,
                    )
                    resp = await asyncio.wait_for(
                        loop.run_in_executor(None, functools.partial(self._clob_client.post_order, signed, OrderType.FOK)),
                        timeout=5.0,
                    )
                else:
                    signed = await asyncio.wait_for(
                        self._clob_client.create_market_order(order_args),
                        timeout=5.0,
                    )
                    resp = await asyncio.wait_for(
                        self._clob_client.post_order(signed, OrderType.FOK),
                        timeout=5.0,
                    )
            except asyncio.TimeoutError:
                logger.error(f"[LIVE] Order placement timed out for token={token_id[:16]}")
                return None
            return OrderResult(
                order_id=resp.get("orderID", "unknown"),
                status=resp.get("status", "UNKNOWN"),
                price=0.0,
                size=amount_usdc,
                side=side,
                token_id=token_id,
            )
        except Exception as exc:
            logger.error(f"Live market order failed: {exc}")
            return None

    async def _live_limit_order(
        self, token_id: str, side: str, price: float, size: float
    ) -> OrderResult | None:
        """Post a real GTC limit order."""
        if not self._clob_client:
            logger.error("ClobClient not initialised — cannot place live limit order")
            return None
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
            )
            try:
                loop = asyncio.get_event_loop()
                if self._clob_is_sync:
                    import functools
                    signed = await asyncio.wait_for(
                        loop.run_in_executor(None, self._clob_client.create_order, order_args),
                        timeout=5.0,
                    )
                    resp = await asyncio.wait_for(
                        loop.run_in_executor(None, functools.partial(self._clob_client.post_order, signed, OrderType.GTC)),
                        timeout=5.0,
                    )
                else:
                    signed = await asyncio.wait_for(
                        self._clob_client.create_order(order_args),
                        timeout=5.0,
                    )
                    resp = await asyncio.wait_for(
                        self._clob_client.post_order(signed, OrderType.GTC),
                        timeout=5.0,
                    )
            except asyncio.TimeoutError:
                logger.error(f"[LIVE] Order placement timed out for token={token_id[:16]}")
                return None
            return OrderResult(
                order_id=resp.get("orderID", "unknown"),
                status=resp.get("status", "LIVE"),
                price=price,
                size=size,
                side=side,
                token_id=token_id,
            )
        except Exception as exc:
            logger.error(f"Live limit order failed: {exc}")
            return None

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Generate HMAC auth headers for CLOB API."""
        import hmac
        import hashlib
        import base64

        ts = str(int(time.time()))
        msg = ts + method.upper() + path
        sig = base64.b64encode(
            hmac.new(
                self.config.api_secret.encode(),
                msg.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return {
            "POLY_ADDRESS": self.config.funder_address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_API_KEY": self.config.api_key,
            "POLY_PASSPHRASE": self.config.api_passphrase,
        }
