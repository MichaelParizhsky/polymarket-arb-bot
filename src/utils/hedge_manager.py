"""
HedgeManager — thin orchestration layer between ArbBot and FuturesHedge.

Tracks which Polymarket token_ids have active futures hedges and provides
a clean async API for opening/closing them from _execute_signals.

===========================================================================
INTEGRATION INSTRUCTIONS FOR main.py
===========================================================================

1. IMPORTS  (add near the top of main.py, with the other src imports)
---------------------------------------------------------------------------
    from src.utils.hedge_manager import HedgeManager
    from src.utils.crypto_detector import detect_crypto_symbol

2. INITIALISE HedgeManager in ArbBot.run()
---------------------------------------------------------------------------
   After the block that creates self._futures_hedge (around line 157),
   add:

        if self._futures_hedge is not None:
            self._hedge_manager = HedgeManager(
                futures_hedge=self._futures_hedge,
                portfolio=self.portfolio,
                config=self.config,
            )
        else:
            self._hedge_manager = None

   Also add to ArbBot.__init__ (alongside self._futures_hedge = None):

        self._hedge_manager: HedgeManager | None = None

3. OPEN HEDGE after a successful BUY in _execute_signals
---------------------------------------------------------------------------
   The current block (around line 444) is:

        if trade:
            trades_total.labels(strategy=sig.strategy, side=sig.side).inc()
            arb_executed.labels(strategy=sig.strategy).inc()
            self._last_trade_time = time.time()
            self._token_last_traded[sig.token_id] = time.time()

   Add the hedge call immediately after for BUY trades only:

        if trade:
            trades_total.labels(strategy=sig.strategy, side=sig.side).inc()
            arb_executed.labels(strategy=sig.strategy).inc()
            self._last_trade_time = time.time()
            self._token_last_traded[sig.token_id] = time.time()
            # Auto-hedge crypto BUY positions via Binance perpetual futures
            if sig.side == "BUY" and self._hedge_manager is not None:
                market_question, _ = self._find_market_info(sig.token_id, context)
                market_tags = self._find_market_tags(sig.token_id, context)
                await self._hedge_manager.maybe_open_hedge(
                    token_id=sig.token_id,
                    market_question=market_question,
                    market_tags=market_tags,
                    side=sig.side,
                    size_usdc=sig.size_usdc,
                )

4. ADD _find_market_tags helper to ArbBot
---------------------------------------------------------------------------
   Place next to _find_market_info (around line 450):

        def _find_market_tags(
            self, token_id: str, context: dict
        ) -> list[str]:
            for m in context.get("markets", []):
                for t in m.tokens:
                    if t.token_id == token_id:
                        return list(getattr(m, "tags", []))
            return []

5. CLOSE HEDGE when a position is closed in _auto_close_resolved_loop
---------------------------------------------------------------------------
   In _auto_close_resolved_loop, after each successful portfolio.sell() call
   that yields a trade (both winner and loser branches), add:

        if trade and self._hedge_manager is not None:
            await self._hedge_manager.maybe_close_hedge(token_id)

   Example — winner branch becomes:
        trade = self.portfolio.sell(...)
        if trade:
            logger.info(...)
            if self._hedge_manager is not None:
                await self._hedge_manager.maybe_close_hedge(token_id)

   Apply the same pattern to the loser branch.

6. OPTIONAL — expose hedge status on the dashboard
---------------------------------------------------------------------------
   In any status/summary method, call:
        if self._hedge_manager:
            hedge_status = self._hedge_manager.get_status()

===========================================================================
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.utils.crypto_detector import detect_crypto_symbol
from src.utils.logger import logger

if TYPE_CHECKING:
    from src.strategies.futures_hedge import FuturesHedge
    from src.portfolio.paper_trading import PaperPortfolio


class HedgeManager:
    """
    Thin orchestration layer between ArbBot and FuturesHedge.

    Responsibilities
    ----------------
    - Detect whether a traded market is a crypto price market.
    - Open a Binance perpetual futures hedge after a successful BUY.
    - Track token_id -> hedge_id mapping so hedges survive across cycles.
    - Close the hedge when the Polymarket position is closed.

    Parameters
    ----------
    futures_hedge:
        An initialised FuturesHedge instance (paper or live).
    portfolio:
        The bot's PaperPortfolio (used for guard checks only; not mutated).
    config:
        Bot Config object.  Expected attributes:
          - config.strategies.futures_hedge_enabled (bool)
          - config.binance.futures_enabled (bool)  [checked as belt-and-suspenders]
          - config.strategies.hedge_ratio (float, optional, default 0.3)
    """

    def __init__(
        self,
        futures_hedge: "FuturesHedge",
        portfolio: "PaperPortfolio",
        config: Any,
    ) -> None:
        self._hedge = futures_hedge
        self._portfolio = portfolio
        self._config = config

        # token_id -> hedge_id for open positions
        self._active: dict[str, str] = {}

        logger.info("[HedgeManager] Initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def maybe_open_hedge(
        self,
        token_id: str,
        market_question: str,
        market_tags: list[str],
        side: str,
        size_usdc: float,
    ) -> None:
        """
        Open a futures hedge for a newly executed BUY trade if:

        - Futures hedging is enabled in config.
        - The market is a supported crypto price prediction market.
        - No hedge is already open for this token_id.

        Parameters
        ----------
        token_id:
            Polymarket token ID of the traded contract.
        market_question:
            Full question text of the market (used for crypto detection).
        market_tags:
            Tag list from the market object (used for crypto detection).
        side:
            Trade side — only "BUY" triggers a hedge.
        size_usdc:
            USDC notional of the Polymarket position.
        """
        if not self._is_enabled():
            return

        if side.upper() != "BUY":
            return

        if token_id in self._active:
            logger.debug(
                f"[HedgeManager] Already hedged {token_id[:16]} — skipping"
            )
            return

        symbol = detect_crypto_symbol(market_question, market_tags)
        if symbol is None:
            logger.debug(
                f"[HedgeManager] Not a crypto market: {market_question[:60]!r}"
            )
            return

        hedge_ratio = self._hedge_ratio()
        logger.info(
            f"[HedgeManager] Opening hedge | token={token_id[:16]} "
            f"symbol={symbol} size=${size_usdc:.2f} ratio={hedge_ratio}"
        )

        try:
            hedge_id = await self._hedge.open_hedge(
                binance_symbol=symbol,
                side=side,
                notional_usdc=size_usdc,
                hedge_ratio=hedge_ratio,
            )
        except Exception as exc:
            logger.error(f"[HedgeManager] open_hedge raised: {exc}")
            return

        if hedge_id:
            self._active[token_id] = hedge_id
            logger.info(
                f"[HedgeManager] Hedge opened | hedge_id={hedge_id[:8]} "
                f"token={token_id[:16]} symbol={symbol}"
            )
        else:
            logger.warning(
                f"[HedgeManager] open_hedge returned None for {symbol} — "
                "position is unhedged"
            )

    async def maybe_close_hedge(self, token_id: str) -> None:
        """
        Close the futures hedge associated with token_id, if one exists.

        Safe to call even if no hedge is open — it will be a no-op.

        Parameters
        ----------
        token_id:
            Polymarket token ID whose hedge should be closed.
        """
        hedge_id = self._active.get(token_id)
        if hedge_id is None:
            return

        logger.info(
            f"[HedgeManager] Closing hedge | hedge_id={hedge_id[:8]} "
            f"token={token_id[:16]}"
        )

        try:
            success = await self._hedge.close_hedge(hedge_id)
        except Exception as exc:
            logger.error(f"[HedgeManager] close_hedge raised: {exc}")
            return

        if success:
            del self._active[token_id]
            logger.info(
                f"[HedgeManager] Hedge closed | hedge_id={hedge_id[:8]}"
            )
        else:
            logger.warning(
                f"[HedgeManager] close_hedge failed for hedge_id={hedge_id[:8]} — "
                "keeping in active map to retry later"
            )

    def get_status(self) -> dict:
        """
        Return a summary of currently open hedges.

        Returns
        -------
        dict with keys:
          - ``count``: number of active hedges
          - ``enabled``: whether futures hedging is enabled in config
          - ``hedge_ratio``: configured ratio
          - ``token_ids``: list of hedged token_ids (truncated to 16 chars)
          - ``open_hedges``: raw dict from FuturesHedge.get_open_hedges()
        """
        return {
            "count": len(self._active),
            "enabled": self._is_enabled(),
            "hedge_ratio": self._hedge_ratio(),
            "token_ids": [tid[:16] for tid in self._active],
            "open_hedges": self._hedge.get_open_hedges(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_enabled(self) -> bool:
        """Return True only when both the strategy flag and the Binance flag are on."""
        strategies = getattr(self._config, "strategies", None)
        binance = getattr(self._config, "binance", None)
        strategy_flag = getattr(strategies, "futures_hedge_enabled", False)
        binance_flag = getattr(binance, "futures_enabled", False)
        return bool(strategy_flag and binance_flag)

    def _hedge_ratio(self) -> float:
        """
        Read hedge_ratio from config.strategies if present, else default to 0.3.
        The attribute is optional — FuturesHedge also has its own default.
        """
        strategies = getattr(self._config, "strategies", None)
        return float(getattr(strategies, "hedge_ratio", 0.3))
