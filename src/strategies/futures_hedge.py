"""
Binance Futures Hedging Utility.

When the latency arb strategy buys YES on a crypto prediction market because
Binance pumped, we open a small opposing Binance perpetual futures position
to remove directional risk.  We are not betting on direction — just the
convergence of the Polymarket probability toward the new Binance price.

Usage (paper mode, no keys needed):
    hedge = FuturesHedge(config, paper_trading=True)
    hedge_id = await hedge.open_hedge("BTCUSDT", side="BUY",
                                       notional_usdc=500.0, hedge_ratio=0.3)
    pnl = hedge.estimated_hedge_pnl(hedge_id, current_price=96_000.0)
    await hedge.close_hedge(hedge_id)

Usage (live mode, requires Binance API keys in config):
    hedge = FuturesHedge(config, paper_trading=False)
    ...

Notes:
  - Live mode uses python-binance AsyncClient with USDT-margined perpetual
    futures (BTCUSDT_PERP, etc.).
  - All live futures orders are market orders with FOK time-in-force.
  - Leverage is explicitly set to 1x before placing any order.
  - Position size = notional_usdc * hedge_ratio / entry_price.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _perp_symbol(binance_symbol: str) -> str:
    """
    Convert a spot symbol like "BTCUSDT" to a perpetual futures symbol
    "BTCUSDT" (on Binance the USDT-M perp uses the same ticker on
    /fapi/ endpoints).
    """
    # Binance USDT-M futures use the same symbol string; no suffix needed
    # when using the fapi (futures API) endpoints.
    return binance_symbol.upper()


def _opposite_side(side: str) -> str:
    """If we are LONG the prediction market, SHORT the futures, and vice-versa."""
    return "SELL" if side.upper() == "BUY" else "BUY"


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class FuturesHedge:
    """
    Manages small Binance perpetual futures positions that hedge the
    directional exposure from Polymarket crypto prediction market trades.

    Parameters
    ----------
    config:
        Bot configuration object.  In live mode, expects
        config.binance_api_key and config.binance_api_secret.
    paper_trading:
        If True (default), all orders are simulated in memory.
        No real orders are placed.
    """

    def __init__(self, config: Any, paper_trading: bool = True) -> None:
        self.config = config
        self.paper_trading = paper_trading

        # hedge_id -> hedge record
        self._paper_hedges: dict[str, dict] = {}

        # In live mode, the AsyncClient is lazily initialised on first use
        self._live_client: Any | None = None

        mode = "PAPER" if paper_trading else "LIVE"
        logger.info(f"[FuturesHedge] Initialised in {mode} mode")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def open_hedge(
        self,
        binance_symbol: str,
        side: str,
        notional_usdc: float,
        hedge_ratio: float = 0.3,
    ) -> str | None:
        """
        Open a small opposing Binance perpetual futures position.

        Parameters
        ----------
        binance_symbol:
            E.g. "BTCUSDT".
        side:
            "BUY" means we are long the prediction market → we SHORT
            futures to hedge.  "SELL" means we are short → we BUY futures.
        notional_usdc:
            Size of the Polymarket position in USDC.
        hedge_ratio:
            Fraction of notional to hedge.  0.3 = hedge 30%.

        Returns
        -------
        hedge_id:
            Unique identifier for this hedge, or None on failure.
        """
        futures_side = _opposite_side(side)  # SHORT when prediction market is LONG
        hedge_notional = notional_usdc * hedge_ratio
        perp = _perp_symbol(binance_symbol)

        if self.paper_trading:
            return await self._open_paper_hedge(
                binance_symbol=binance_symbol,
                perp=perp,
                futures_side=futures_side,
                hedge_notional=hedge_notional,
                hedge_ratio=hedge_ratio,
            )
        else:
            return await self._open_live_hedge(
                binance_symbol=binance_symbol,
                perp=perp,
                futures_side=futures_side,
                hedge_notional=hedge_notional,
                hedge_ratio=hedge_ratio,
            )

    async def close_hedge(self, hedge_id: str) -> bool:
        """
        Close an open hedge position by hedge_id.

        Returns True on success, False if the hedge was not found or the
        order could not be placed.
        """
        if self.paper_trading:
            return self._close_paper_hedge(hedge_id)
        else:
            return await self._close_live_hedge(hedge_id)

    def get_open_hedges(self) -> dict[str, dict]:
        """
        Return all currently open hedge positions.

        Each value is a dict with keys:
            symbol, perp, futures_side, entry_price, quantity,
            hedge_notional, opened_at, order_id (live only)
        """
        if self.paper_trading:
            return dict(self._paper_hedges)
        # In live mode we track open hedges in the same dict structure
        return dict(self._paper_hedges)

    def estimated_hedge_pnl(self, hedge_id: str, current_price: float) -> float:
        """
        Estimate the unrealised P&L of a hedge position in USDC.

        P&L = quantity * (current_price - entry_price) * direction
        where direction = +1 for LONG futures, -1 for SHORT futures.

        Parameters
        ----------
        hedge_id:
            The ID returned by open_hedge().
        current_price:
            Current spot/futures price of the underlying.

        Returns
        -------
        Estimated P&L in USDC (positive = profit, negative = loss).
        """
        hedge = self._paper_hedges.get(hedge_id)
        if hedge is None:
            logger.warning(f"[FuturesHedge] hedge_id {hedge_id!r} not found")
            return 0.0

        entry_price: float = hedge["entry_price"]
        quantity: float = hedge["quantity"]
        futures_side: str = hedge["futures_side"]

        # LONG futures profits when price goes up; SHORT profits when it goes down
        direction = 1.0 if futures_side == "BUY" else -1.0
        pnl = quantity * (current_price - entry_price) * direction

        logger.debug(
            f"[FuturesHedge] PnL for {hedge_id[:8]} | "
            f"{futures_side} {quantity:.6f} {hedge['symbol']} | "
            f"entry={entry_price:.2f} current={current_price:.2f} | "
            f"pnl={pnl:+.4f} USDC"
        )
        return pnl

    # ------------------------------------------------------------------
    # Paper trading internals
    # ------------------------------------------------------------------

    async def _open_paper_hedge(
        self,
        binance_symbol: str,
        perp: str,
        futures_side: str,
        hedge_notional: float,
        hedge_ratio: float,
    ) -> str | None:
        """Simulate opening a futures hedge and store in memory."""
        # Fetch a reference price to simulate the fill
        entry_price = await self._get_reference_price(binance_symbol)
        if entry_price is None or entry_price <= 0:
            logger.warning(
                f"[FuturesHedge] Cannot open paper hedge: no price for {binance_symbol}"
            )
            return None

        quantity = hedge_notional / entry_price
        hedge_id = str(uuid.uuid4())

        self._paper_hedges[hedge_id] = {
            "symbol": binance_symbol,
            "perp": perp,
            "futures_side": futures_side,
            "entry_price": entry_price,
            "quantity": quantity,
            "hedge_notional": hedge_notional,
            "hedge_ratio": hedge_ratio,
            "opened_at": time.time(),
            "closed": False,
        }

        side_label = futures_side  # "BUY" or "SELL"
        direction_label = "SHORT" if futures_side == "SELL" else "LONG"
        logger.info(
            f"[PAPER HEDGE] {direction_label} {quantity:.6f} {perp} "
            f"@ {entry_price:.2f} notional=${hedge_notional:.2f}"
        )
        return hedge_id

    def _close_paper_hedge(self, hedge_id: str) -> bool:
        """Remove a paper hedge from the in-memory store."""
        hedge = self._paper_hedges.pop(hedge_id, None)
        if hedge is None:
            logger.warning(f"[FuturesHedge] close_hedge: {hedge_id!r} not found")
            return False
        direction_label = "LONG" if hedge["futures_side"] == "BUY" else "SHORT"
        held_seconds = time.time() - hedge["opened_at"]
        logger.info(
            f"[PAPER HEDGE] CLOSED {direction_label} {hedge['quantity']:.6f} "
            f"{hedge['perp']} (held {held_seconds:.1f}s)"
        )
        return True

    # ------------------------------------------------------------------
    # Live trading internals
    # ------------------------------------------------------------------

    async def _get_live_client(self) -> Any:
        """Lazily initialise the Binance AsyncClient."""
        if self._live_client is not None:
            return self._live_client

        try:
            from binance import AsyncClient  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "python-binance is required for live futures hedging. "
                "Install it with: pip install python-binance"
            ) from exc

        api_key: str = getattr(self.config, "binance_api_key", "")
        api_secret: str = getattr(self.config, "binance_api_secret", "")

        if not api_key or not api_secret:
            raise RuntimeError(
                "Binance API key/secret not configured. "
                "Set config.binance_api_key and config.binance_api_secret."
            )

        self._live_client = await AsyncClient.create(api_key, api_secret)
        logger.info("[FuturesHedge] Binance AsyncClient initialised (live mode)")
        return self._live_client

    async def _open_live_hedge(
        self,
        binance_symbol: str,
        perp: str,
        futures_side: str,
        hedge_notional: float,
        hedge_ratio: float,
    ) -> str | None:
        """
        Place a real market order on Binance USDT-M perpetual futures.

        Steps:
          1. Set leverage to 1x for this symbol
          2. Get current mark price
          3. Calculate quantity = hedge_notional / mark_price
          4. Place FOK market order
          5. Record hedge locally
        """
        try:
            client = await self._get_live_client()

            # 1. Set leverage to 1x
            await client.futures_change_leverage(
                symbol=perp, leverage=1
            )
            logger.debug(f"[FuturesHedge] Leverage set to 1x for {perp}")

            # 2. Get current mark price
            mark_info = await client.futures_mark_price(symbol=perp)
            entry_price = float(mark_info["markPrice"])
            if entry_price <= 0:
                logger.error(f"[FuturesHedge] Invalid mark price for {perp}: {entry_price}")
                return None

            # 3. Calculate quantity (round to 3 decimal places for most perps)
            quantity = round(hedge_notional / entry_price, 3)
            if quantity <= 0:
                logger.warning(
                    f"[FuturesHedge] Computed quantity {quantity} too small for {perp}"
                )
                return None

            # 4. Place FOK market order
            order = await client.futures_create_order(
                symbol=perp,
                side=futures_side,
                type="MARKET",
                quantity=quantity,
                timeInForce="FOK",
                reduceOnly=False,
            )

            order_id = str(order.get("orderId", ""))
            fill_price = float(order.get("avgPrice", entry_price) or entry_price)

            hedge_id = str(uuid.uuid4())
            self._paper_hedges[hedge_id] = {
                "symbol": binance_symbol,
                "perp": perp,
                "futures_side": futures_side,
                "entry_price": fill_price,
                "quantity": quantity,
                "hedge_notional": hedge_notional,
                "hedge_ratio": hedge_ratio,
                "opened_at": time.time(),
                "order_id": order_id,
                "closed": False,
            }

            direction_label = "SHORT" if futures_side == "SELL" else "LONG"
            logger.info(
                f"[LIVE HEDGE] {direction_label} {quantity} {perp} "
                f"@ {fill_price:.2f} notional=${hedge_notional:.2f} "
                f"order_id={order_id}"
            )
            return hedge_id

        except Exception as exc:
            logger.error(f"[FuturesHedge] Failed to open live hedge for {perp}: {exc}")
            return None

    async def _close_live_hedge(self, hedge_id: str) -> bool:
        """Place an opposing market order to close the hedge position."""
        hedge = self._paper_hedges.get(hedge_id)
        if hedge is None:
            logger.warning(f"[FuturesHedge] close_hedge: {hedge_id!r} not found")
            return False

        close_side = _opposite_side(hedge["futures_side"])
        perp = hedge["perp"]
        quantity = hedge["quantity"]

        try:
            client = await self._get_live_client()

            order = await client.futures_create_order(
                symbol=perp,
                side=close_side,
                type="MARKET",
                quantity=quantity,
                timeInForce="FOK",
                reduceOnly=True,
            )

            fill_price = float(order.get("avgPrice", 0) or 0)
            order_id = str(order.get("orderId", ""))
            direction_label = "LONG" if hedge["futures_side"] == "BUY" else "SHORT"
            held_seconds = time.time() - hedge["opened_at"]

            logger.info(
                f"[LIVE HEDGE] CLOSED {direction_label} {quantity} {perp} "
                f"@ {fill_price:.2f} (held {held_seconds:.1f}s) "
                f"close_order_id={order_id}"
            )

            del self._paper_hedges[hedge_id]
            return True

        except Exception as exc:
            logger.error(
                f"[FuturesHedge] Failed to close live hedge {hedge_id!r} for {perp}: {exc}"
            )
            return False

    # ------------------------------------------------------------------
    # Price helper
    # ------------------------------------------------------------------

    async def _get_reference_price(self, binance_symbol: str) -> float | None:
        """
        Get a reference price for position-sizing.

        In paper mode, tries the Binance REST API.  Falls back to None if
        the request fails (paper trades will be skipped rather than guessing).
        """
        try:
            import httpx  # type: ignore[import]
            url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={binance_symbol.upper()}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                price = float(data["price"])
                logger.debug(
                    f"[FuturesHedge] Reference price for {binance_symbol}: {price:.2f}"
                )
                return price
        except Exception as exc:
            logger.warning(
                f"[FuturesHedge] Could not fetch reference price for "
                f"{binance_symbol}: {exc}"
            )
            # Fall back to the BinanceFeed cache if accessible via context
            return None
