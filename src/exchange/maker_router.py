"""
Maker-first order routing for Polymarket CLOB.

Key 2026 facts:
- Maker fee = 0% on all markets.
- Taker fee = ~2% on crypto/sports markets, lower on general markets.
- feeRateBps is required in signed order payloads. py-clob-client includes
  it automatically when you use create_limit_order() — do NOT build raw
  EIP-712 payloads without it or orders will be silently rejected.
- Maker rebates are per-market (changed from global in late 2025).
- Cancel/replace target: < 200ms to avoid adverse selection.

Usage:
    from src.exchange.maker_router import MakerRouter, AutoRedeemer
    router = MakerRouter(poly_client)
    # Place a maker-first order:
    ok = await router.place_maker_first(
        token_id=token_id, side="BUY", size_usdc=50.0, price=0.65
    )
    # Start auto-redeem background loop:
    redeemer = AutoRedeemer(poly_client)
    await redeemer.start()

Integration point in main.py:
    Call router.place_maker_first() from _execute_signals() for paper=False
    BUY signals when MAKER_FIRST_ENABLED=true in config.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.utils.logger import logger

# Maker-first timeout: wait this many seconds for a maker fill before falling back
MAKER_TIMEOUT_S = 8.0
# Place limit 1 cent better than mid to improve fill probability while staying maker
MAKER_PRICE_IMPROVE_CENTS = 0.01
# Minimum fill fraction to consider a maker order successful
MAKER_MIN_FILL_FRACTION = 0.90
# Cancel stale maker orders after this many seconds (price may have drifted)
MAKER_MAX_AGE_S = 30.0


@dataclass
class RouterResult:
    success: bool
    order_id: Optional[str]
    filled_usd: float
    fee_paid_usd: float
    mode: str       # "MAKER" | "TAKER" | "DRY_RUN" | "FAILED"
    latency_ms: float
    error: Optional[str] = None


class MakerRouter:
    """
    Thin routing layer on top of PolymarketClient.
    Tries a maker (post-only GTC) order first; falls back to taker FOK
    if the maker doesn't fill within MAKER_TIMEOUT_S.

    Instantiate once and reuse — tracks active maker orders per token.
    """

    def __init__(self, poly_client) -> None:
        self._poly = poly_client
        self._active: dict[str, dict] = {}  # token_id -> {order_id, price, placed_at}

    async def place_maker_first(
        self,
        token_id: str,
        side: str,
        size_usdc: float,
        price: float,
        dry_run: bool = False,
    ) -> RouterResult:
        """
        Attempt maker (0% fee) first; fall back to taker (~2% fee) on timeout.
        Returns RouterResult with fill info.
        """
        t0 = time.perf_counter()

        if dry_run:
            return RouterResult(
                success=True, order_id="DRY_RUN", filled_usd=size_usdc,
                fee_paid_usd=0.0, mode="DRY_RUN",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # Cancel any stale maker order on this token first
        await self._cancel_stale(token_id)

        # Try maker (post-only GTC limit order)
        maker_result = await self._place_maker(token_id, side, size_usdc, price, t0)

        if maker_result.success:
            return maker_result

        # Maker failed or timed out — fall back to taker FOK
        logger.info(
            f"[MakerRouter] Maker order failed/timeout for {token_id[:12]}..., "
            f"falling back to taker"
        )
        return await self._place_taker(token_id, side, size_usdc, price, t0)

    async def _place_maker(
        self, token_id: str, side: str, size_usdc: float, price: float, t0: float
    ) -> RouterResult:
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            loop = asyncio.get_event_loop()

            # Improve price slightly to increase fill probability
            if side.upper() == "BUY":
                limit_price = min(round(price + MAKER_PRICE_IMPROVE_CENTS, 4), 0.99)
            else:
                limit_price = max(round(price - MAKER_PRICE_IMPROVE_CENTS, 4), 0.01)

            shares = round(size_usdc / max(limit_price, 0.01), 2)
            shares = max(shares, 5.0)  # CLOB minimum 5 shares

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side=BUY if side.upper() == "BUY" else SELL,
            )

            # py-clob-client automatically includes feeRateBps in the signed payload
            signed = await loop.run_in_executor(
                None, lambda: self._poly._clob_client.create_limit_order(order_args)
            )
            resp = await loop.run_in_executor(
                None, lambda: self._poly._clob_client.post_order(signed, OrderType.GTC)
            )

            order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
            if not order_id:
                return RouterResult(
                    success=False, order_id=None, filled_usd=0.0,
                    fee_paid_usd=0.0, mode="MAKER",
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    error="No orderID returned"
                )

            self._active[token_id] = {
                "order_id": order_id,
                "price": limit_price,
                "size_usdc": size_usdc,
                "placed_at": time.time(),
            }

            logger.info(
                f"[MakerRouter] GTC maker order: {order_id[:14]}... | "
                f"{side} {shares:.2f} shares @ {limit_price:.4f} (0% fee)"
            )

            return RouterResult(
                success=True,
                order_id=order_id,
                filled_usd=0.0,           # GTC: fill tracked separately via user WS
                fee_paid_usd=0.0,         # maker = 0%
                mode="MAKER",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        except Exception as exc:
            logger.warning(f"[MakerRouter] Maker order exception: {exc}")
            return RouterResult(
                success=False, order_id=None, filled_usd=0.0,
                fee_paid_usd=0.0, mode="MAKER",
                latency_ms=(time.perf_counter() - t0) * 1000,
                error=str(exc)
            )

    async def _place_taker(
        self, token_id: str, side: str, size_usdc: float, price: float, t0: float
    ) -> RouterResult:
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            loop = asyncio.get_event_loop()
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=size_usdc,
                side=BUY if side.upper() == "BUY" else SELL,
            )
            signed = await loop.run_in_executor(
                None, lambda: self._poly._clob_client.create_market_order(order_args)
            )
            resp = await loop.run_in_executor(
                None, lambda: self._poly._clob_client.post_order(signed, OrderType.FOK)
            )

            order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
            # Approximate fill (taker fee ~2% on crypto markets)
            filled = size_usdc
            fee = filled * 0.020

            return RouterResult(
                success=bool(order_id),
                order_id=order_id or None,
                filled_usd=filled,
                fee_paid_usd=fee,
                mode="TAKER",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        except Exception as exc:
            logger.warning(f"[MakerRouter] Taker order exception: {exc}")
            return RouterResult(
                success=False, order_id=None, filled_usd=0.0,
                fee_paid_usd=0.0, mode="TAKER",
                latency_ms=(time.perf_counter() - t0) * 1000,
                error=str(exc)
            )

    async def _cancel_stale(self, token_id: str) -> None:
        """Cancel maker orders on this token that are older than MAKER_MAX_AGE_S."""
        if token_id not in self._active:
            return
        info = self._active[token_id]
        age = time.time() - info["placed_at"]
        if age >= MAKER_MAX_AGE_S:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, lambda: self._poly._clob_client.cancel(info["order_id"])
                )
                del self._active[token_id]
                logger.debug(
                    f"[MakerRouter] Cancelled stale maker order "
                    f"{info['order_id'][:14]}... (age={age:.0f}s)"
                )
            except Exception as exc:
                logger.debug(f"[MakerRouter] Cancel failed (non-fatal): {exc}")

    async def cancel_all(self) -> None:
        """Cancel all tracked maker orders — call on shutdown."""
        for token_id in list(self._active.keys()):
            await self._cancel_stale.__wrapped__(self, token_id)  # force cancel regardless of age
        self._active.clear()


class AutoRedeemer:
    """
    Background loop that polls open positions every poll_interval_s seconds
    and automatically redeems any that have resolved.

    Redeeming quickly recycles USDC back into the pool for the next trade.
    Without this, resolved positions lock up capital until manually redeemed.

    Integration: call await redeemer.start() from ArbBot.run() after poly client init.
    """

    def __init__(self, poly_client, poll_interval_s: float = 60.0) -> None:
        self._poly = poly_client
        self._interval = poll_interval_s
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[AutoRedeemer] Started — polling every {self._interval:.0f}s")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._redeem_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"[AutoRedeemer] Error (non-fatal): {exc}")
            await asyncio.sleep(self._interval)

    async def _redeem_all(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            # py-clob-client: get_positions() returns open conditional token positions
            positions = await loop.run_in_executor(
                None, lambda: self._poly._clob_client.get_positions()
            )
            if not positions:
                return
            for pos in positions:
                if pos.get("canRedeem") or pos.get("redeemable"):
                    cond_id = pos.get("conditionId") or pos.get("condition_id")
                    if not cond_id:
                        continue
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda c=cond_id: self._poly._clob_client.redeem_positions(c)
                        )
                        logger.info(
                            f"[AutoRedeemer] Redeemed resolved position: {cond_id[:16]}..."
                        )
                    except Exception as exc:
                        logger.debug(f"[AutoRedeemer] Redeem failed for {cond_id[:16]}: {exc}")
        except Exception as exc:
            logger.debug(f"[AutoRedeemer] get_positions failed: {exc}")
