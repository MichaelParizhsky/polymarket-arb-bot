"""
Binance price feed via WebSocket.
Maintains a real-time price cache for crypto symbols used in latency arb.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable

import websockets
from tenacity import retry, stop_after_attempt, wait_exponential

from src.utils.logger import logger

# Polymarket crypto prediction market tags -> Binance symbols
SYMBOL_MAP: dict[str, str] = {
    "btc": "BTCUSDT",
    "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT",
    "ethereum": "ETHUSDT",
    "sol": "SOLUSDT",
    "solana": "SOLUSDT",
    "bnb": "BNBUSDT",
    "doge": "DOGEUSDT",
    "dogecoin": "DOGEUSDT",
    "xrp": "XRPUSDT",
    "ripple": "XRPUSDT",
    "matic": "MATICUSDT",
    "polygon": "MATICUSDT",
    "link": "LINKUSDT",
    "chainlink": "LINKUSDT",
    "avax": "AVAXUSDT",
    "avalanche": "AVAXUSDT",
}


@dataclass
class PriceTick:
    symbol: str
    price: float
    timestamp: float = field(default_factory=time.time)
    bid: float = 0.0
    ask: float = 0.0


class BinanceFeed:
    """
    WebSocket-based Binance price feed.
    Subscribes to bookTicker streams for low-latency bid/ask updates.
    """

    WS_BASE = "wss://stream.binance.com:9443/stream"

    def __init__(self, symbols: list[str] | None = None, reconnect_delay: int = 5) -> None:
        self._symbols = [s.lower() for s in (symbols or list(set(SYMBOL_MAP.values())))]
        self._reconnect_delay = reconnect_delay
        self._prices: dict[str, PriceTick] = {}
        self._callbacks: list[Callable[[PriceTick], None]] = []
        self._running = False
        self._ws_task: asyncio.Task | None = None

    def subscribe(self, callback: Callable[[PriceTick], None]) -> None:
        self._callbacks.append(callback)

    def get_price(self, symbol: str) -> PriceTick | None:
        return self._prices.get(symbol.upper())

    def get_price_for_keyword(self, keyword: str) -> PriceTick | None:
        """Look up price by Polymarket keyword (e.g. 'bitcoin', 'btc')."""
        sym = SYMBOL_MAP.get(keyword.lower())
        if sym:
            return self._prices.get(sym)
        return None

    def is_stale(self, symbol: str, max_age_seconds: float = 5.0) -> bool:
        tick = self._prices.get(symbol.upper())
        if not tick:
            return True
        return (time.time() - tick.timestamp) > max_age_seconds

    async def start(self) -> None:
        self._running = True
        self._ws_task = asyncio.create_task(self._run_forever())
        logger.info(f"Binance feed started for {len(self._symbols)} symbols")

    async def stop(self) -> None:
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        fail_count = 0
        while self._running:
            try:
                await self._connect()
                fail_count = 0
            except Exception as exc:
                fail_count += 1
                if fail_count >= 3:
                    logger.warning("Binance WS unavailable — running without live price feed. Latency arb disabled.")
                    # Poll via REST every 30s as fallback
                    while self._running:
                        await self.fetch_snapshot()
                        await asyncio.sleep(30)
                    return
                logger.warning(f"Binance WS disconnected: {exc}. Reconnecting in {self._reconnect_delay}s")
                await asyncio.sleep(self._reconnect_delay)

    async def _connect(self) -> None:
        streams = "/".join(f"{s}@bookTicker" for s in self._symbols)
        url = f"{self.WS_BASE}?streams={streams}"
        logger.debug(f"Connecting to Binance WS: {url[:80]}...")

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            logger.info("Binance WS connected")
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    self._handle_message(msg)
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.debug(f"Bad WS message: {exc}")

    def _handle_message(self, msg: dict) -> None:
        data = msg.get("data", msg)
        if "s" not in data:
            return

        symbol = data["s"]
        bid = float(data.get("b", 0))
        ask = float(data.get("a", 0))
        price = (bid + ask) / 2 if bid and ask else float(data.get("c", 0))

        tick = PriceTick(symbol=symbol, price=price, bid=bid, ask=ask)
        self._prices[symbol] = tick

        for cb in self._callbacks:
            try:
                cb(tick)
            except Exception as exc:
                logger.debug(f"Price callback error: {exc}")

    async def fetch_snapshot(self) -> dict[str, float]:
        """
        Fetch current prices via REST as a fallback / initial snapshot.
        """
        import httpx
        results = {}
        symbols_upper = [s.upper() for s in self._symbols]
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.binance.com/api/v3/ticker/bookTicker",
                )
                resp.raise_for_status()
                for item in resp.json():
                    if item["symbol"] in symbols_upper:
                        bid = float(item["bidPrice"])
                        ask = float(item["askPrice"])
                        price = (bid + ask) / 2
                        symbol = item["symbol"]
                        tick = PriceTick(symbol=symbol, price=price, bid=bid, ask=ask)
                        self._prices[symbol] = tick
                        results[symbol] = price
            logger.info(f"Binance REST snapshot: {len(results)} prices")
        except Exception as exc:
            logger.warning(f"Binance REST snapshot failed: {exc}")
        return results
