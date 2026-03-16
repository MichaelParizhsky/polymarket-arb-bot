"""
data/price_feed.py — Real-time crypto price feed from Binance WebSocket
Used by the latency arb strategy to detect when Polymarket lags.
"""
from __future__ import annotations
import asyncio
import json
import os
from typing import Dict, Optional, Callable
from datetime import datetime

import websockets
from loguru import logger


BINANCE_WS = os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/stream")
SYMBOLS    = [s.strip().upper() for s in os.getenv("LATENCY_ARB_SYMBOLS", "BTC,ETH,SOL").split(",")]


class BinancePriceFeed:
    """
    Subscribes to Binance mini-ticker WebSocket streams for BTC, ETH, SOL.
    Maintains an in-memory price dict, pushes updates to registered callbacks.
    """

    def __init__(self):
        self.prices:     Dict[str, float]    = {}
        self.timestamps: Dict[str, datetime] = {}
        self._callbacks: list[Callable]      = []
        self._running = False

    def register_callback(self, fn: Callable) -> None:
        self._callbacks.append(fn)

    def get_price(self, symbol: str) -> Optional[float]:
        return self.prices.get(symbol.upper())

    def get_age_ms(self, symbol: str) -> Optional[float]:
        ts = self.timestamps.get(symbol.upper())
        if not ts:
            return None
        return (datetime.utcnow() - ts).total_seconds() * 1000

    async def run(self) -> None:
        """Main loop — reconnects on failure."""
        self._running = True
        streams = "/".join(f"{s.lower()}usdt@miniTicker" for s in SYMBOLS)
        url = f"{BINANCE_WS}?streams={streams}"

        while self._running:
            try:
                logger.info(f"[PriceFeed] Connecting to Binance: {streams}")
                async with websockets.connect(url, ping_interval=20) as ws:
                    async for raw in ws:
                        self._handle(raw)
            except Exception as e:
                logger.warning(f"[PriceFeed] Disconnected ({e}), retrying in 3s...")
                await asyncio.sleep(3)

    def _handle(self, raw: str) -> None:
        try:
            envelope = json.loads(raw)
            data     = envelope.get("data", envelope)
            symbol   = data.get("s", "").replace("USDT", "").upper()
            price    = float(data.get("c", 0))   # "c" = close/last price

            if symbol and price:
                self.prices[symbol]     = price
                self.timestamps[symbol] = datetime.utcnow()
                for cb in self._callbacks:
                    try:
                        cb(symbol, price)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[PriceFeed] Parse error: {e}")

    def stop(self) -> None:
        self._running = False
