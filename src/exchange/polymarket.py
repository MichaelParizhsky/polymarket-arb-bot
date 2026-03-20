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


@dataclass
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


@dataclass
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

    def __init__(self, config, paper_trading: bool = True) -> None:
        self.config = config
        self.paper_trading = paper_trading
        self._http: httpx.AsyncClient | None = None
        self._market_cache: dict[str, Market] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 30.0  # seconds
        self._clob_client = None  # initialised once in __aenter__ for live mode

    async def __aenter__(self) -> "PolymarketClient":
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": "polymarket-arb-bot/1.0"},
        )
        # Pre-build the ClobClient once for the session to avoid per-order overhead
        if not self.paper_trading and self.config.private_key:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.constants import POLYGON
                self._clob_client = ClobClient(
                    host=self.CLOB_BASE,
                    chain_id=POLYGON,
                    key=self.config.private_key,
                    creds={
                        "api_key": self.config.api_key,
                        "api_secret": self.config.api_secret,
                        "api_passphrase": self.config.api_passphrase,
                    },
                    signature_type=2,
                    funder=self.config.funder_address,
                )
                logger.info("ClobClient initialised (session-level)")
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
            try:
                # API uses clobTokenIds + outcomes arrays
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
                    continue

                condition_id = raw.get("conditionId") or raw.get("id") or ""
                markets.append(Market(
                    condition_id=str(condition_id),
                    question=raw.get("question", ""),
                    tokens=tokens,
                    active=raw.get("active", False),
                    closed=raw.get("closed", True),
                    end_date_iso=raw.get("endDateIso") or raw.get("endDate", ""),
                    tags=[raw.get("category", "")] if raw.get("category") else [],
                    volume=float(raw.get("volumeNum") or raw.get("volume") or 0),
                    liquidity=float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug(f"Skipping malformed market: {exc}")
        return markets

    async def get_markets_cached(self) -> list[Market]:
        """Return cached markets, refreshing if stale."""
        now = time.time()
        if now - self._cache_ts > self._cache_ttl or not self._market_cache:
            markets = await self.get_markets(limit=500)
            self._market_cache = {m.condition_id: m for m in markets}
            self._cache_ts = now
            logger.debug(f"Refreshed market cache: {len(self._market_cache)} markets")
        return list(self._market_cache.values())

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
        """Simulate a limit order placement."""
        order_id = f"paper_lim_{token_id[:8]}_{int(time.time()*1000)}"
        logger.info(
            f"[PAPER] LIMIT {side} {size:.2f} contracts @ {price:.4f} "
            f"token={token_id[:16]}..."
        )
        return OrderResult(
            order_id=order_id,
            status="LIVE",
            price=price,
            size=size,
            side=side,
            token_id=token_id,
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
            loop = asyncio.get_running_loop()
            order_args = MarketOrderArgs(token_id=token_id, amount=amount_usdc)
            signed = await loop.run_in_executor(
                None, self._clob_client.create_market_order, order_args
            )
            resp = await loop.run_in_executor(
                None, self._clob_client.post_order, signed, OrderType.FOK
            )
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
            from py_clob_client.clob_types import LimitOrderArgs, OrderType
            from py_clob_client.constants import BUY, SELL

            order_args = LimitOrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY if side == "BUY" else SELL,
            )
            loop = asyncio.get_running_loop()
            signed = await loop.run_in_executor(None, self._clob_client.create_order, order_args)
            resp = await loop.run_in_executor(None, self._clob_client.post_order, signed, OrderType.GTC)
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
