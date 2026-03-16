"""
Strategy: Resolution Trading.

Near-expiry markets that are strongly trending toward YES or NO resolution
can offer near-guaranteed returns when the ask is still slightly below $1.00.

Logic:
  - Scan markets whose end_date_iso is within 48 hours.
  - If YES price is 0.80–0.97 AND market expires within 24 h AND best_ask < 0.96,
    buy YES.  Net edge = (1.00 - ask) - fees.  Minimum net edge: 2%.
  - If YES price is 0.03–0.20 (i.e. NO price is 0.80–0.97) AND market expires
    within 24 h AND NO best_ask < 0.96, buy NO.
  - Only enter once per market (tracked in self._resolution_positions).
  - Position sizing via risk_manager.size_position() with a bespoke base_size.
"""
from __future__ import annotations

import time
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.strategies.latency_arb import _days_to_expiry
from src.utils.metrics import arb_opportunities, edge_detected

# ------------------------------------------------------------------ #
#  Constants                                                           #
# ------------------------------------------------------------------ #

FEE_RATE = 0.002            # 0.2% per leg (one-way)

# Resolution likelihood window: prices outside this range are ignored
YES_LIKELY_LOW = 0.80       # YES must be at least this price …
YES_LIKELY_HIGH = 0.97      # … but not already at this ceiling
YES_ASK_MAX = 0.96          # we only buy if ask is below this (≥4% edge gross)

NO_LIKELY_LOW = 0.03        # YES price must be at most this (NO strongly likely)
NO_LIKELY_HIGH = 0.20       # YES price must be at most this
NO_ASK_MAX = 0.96           # same cap on NO ask

HOURS_THRESHOLD_OUTER = 48.0   # only consider markets closing within this window
HOURS_THRESHOLD_INNER = 24.0   # only trade markets closing within this window
MIN_NET_EDGE = 0.02             # minimum required net edge after fees


# ------------------------------------------------------------------ #
#  Strategy                                                            #
# ------------------------------------------------------------------ #

class ResolutionStrategy(BaseStrategy):
    """
    Near-expiry resolution trading strategy.

    Buys YES (or NO) on markets that are strongly trending toward a
    resolution outcome with less than 24 hours remaining, provided the
    ask price is low enough to guarantee at least MIN_NET_EDGE after fees.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        # condition_id -> entry price; prevents doubling up on the same market
        self._resolution_positions: dict[str, float] = {}

    # ---------------------------------------------------------------- #
    #  Main scan                                                         #
    # ---------------------------------------------------------------- #

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        signals: list[Signal] = []

        for market in markets:
            if not market.active or market.closed:
                continue

            # Only look at markets expiring within the outer window
            dte_days = _days_to_expiry(market.end_date_iso)
            hours_left = dte_days * 24.0
            if hours_left > HOURS_THRESHOLD_OUTER:
                continue

            # We only trade inside the inner window (< 24 h)
            if hours_left > HOURS_THRESHOLD_INNER:
                continue

            # Skip if we already have a resolution position on this market
            if market.condition_id in self._resolution_positions:
                continue

            sig = self._evaluate_market(market, orderbooks, hours_left)
            if sig is not None:
                signals.append(sig)

        return signals

    # ---------------------------------------------------------------- #
    #  Per-market evaluation                                             #
    # ---------------------------------------------------------------- #

    def _evaluate_market(
        self,
        market: Market,
        orderbooks: dict[str, Orderbook],
        hours_left: float,
    ) -> Signal | None:
        """
        Evaluate a single market for a resolution trade.
        Returns a Signal if an actionable opportunity exists, else None.
        """
        yes_token = next(
            (t for t in market.tokens if t.outcome.lower() == "yes"), None
        )
        no_token = next(
            (t for t in market.tokens if t.outcome.lower() == "no"), None
        )
        if not yes_token or not no_token:
            return None

        yes_book = orderbooks.get(yes_token.token_id)
        no_book = orderbooks.get(no_token.token_id)

        # Need at least the YES mid to assess probability
        if not yes_book or yes_book.mid is None:
            return None

        yes_mid = yes_book.mid

        # --- YES resolution path ---
        if YES_LIKELY_LOW <= yes_mid <= YES_LIKELY_HIGH:
            return self._check_yes_entry(
                market=market,
                yes_token_id=yes_token.token_id,
                yes_book=yes_book,
                yes_mid=yes_mid,
                hours_left=hours_left,
            )

        # --- NO resolution path ---
        if NO_LIKELY_LOW <= yes_mid <= NO_LIKELY_HIGH:
            if no_book is None:
                return None
            no_mid = no_book.mid
            if no_mid is None:
                return None
            return self._check_no_entry(
                market=market,
                no_token_id=no_token.token_id,
                no_book=no_book,
                no_mid=no_mid,
                yes_mid=yes_mid,
                hours_left=hours_left,
            )

        return None

    # ---------------------------------------------------------------- #
    #  YES entry check                                                   #
    # ---------------------------------------------------------------- #

    def _check_yes_entry(
        self,
        market: Market,
        yes_token_id: str,
        yes_book: Orderbook,
        yes_mid: float,
        hours_left: float,
    ) -> Signal | None:
        best_ask = yes_book.best_ask
        if best_ask is None:
            return None
        if best_ask >= YES_ASK_MAX:
            return None

        # Net edge = (resolution payout $1.00) – ask – fee
        gross_edge = 1.0 - best_ask
        net_edge = gross_edge - FEE_RATE

        if net_edge < MIN_NET_EDGE:
            return None

        arb_opportunities.labels(strategy="resolution").inc()
        edge_detected.labels(strategy="resolution").observe(net_edge)

        base_size = getattr(
            self.config.strategies, "rebalancing_max_spend", 50.0
        ) / 2.0
        size_usdc = self.risk.size_position(edge=net_edge, base_size=base_size)
        if size_usdc < 1.0:
            return None

        self.log(
            f"[RESOLUTION] BUY YES @ {best_ask:.4f} | "
            f"market resolves in {hours_left:.1f}h | "
            f"yes_mid={yes_mid:.3f} | edge={net_edge:.3f} | "
            f"size=${size_usdc:.2f} | q={market.question[:60]}"
        )

        self._resolution_positions[market.condition_id] = best_ask

        return Signal(
            strategy="resolution",
            token_id=yes_token_id,
            side="BUY",
            price=best_ask,
            size_usdc=size_usdc,
            edge=net_edge,
            notes=(
                f"[RESOLUTION] BUY YES @ {best_ask:.4f} | "
                f"market resolves in {hours_left:.1f}h | "
                f"edge={net_edge:.3f}"
            ),
            metadata={
                "strategy": "resolution",
                "outcome": "YES",
                "yes_mid": yes_mid,
                "hours_left": hours_left,
                "gross_edge": gross_edge,
                "net_edge": net_edge,
                "condition_id": market.condition_id,
                "entered_at": time.time(),
            },
        )

    # ---------------------------------------------------------------- #
    #  NO entry check                                                    #
    # ---------------------------------------------------------------- #

    def _check_no_entry(
        self,
        market: Market,
        no_token_id: str,
        no_book: Orderbook,
        no_mid: float,
        yes_mid: float,
        hours_left: float,
    ) -> Signal | None:
        best_ask = no_book.best_ask
        if best_ask is None:
            return None
        if best_ask >= NO_ASK_MAX:
            return None

        # Net edge = (resolution payout $1.00) – ask – fee
        gross_edge = 1.0 - best_ask
        net_edge = gross_edge - FEE_RATE

        if net_edge < MIN_NET_EDGE:
            return None

        arb_opportunities.labels(strategy="resolution").inc()
        edge_detected.labels(strategy="resolution").observe(net_edge)

        base_size = getattr(
            self.config.strategies, "rebalancing_max_spend", 50.0
        ) / 2.0
        size_usdc = self.risk.size_position(edge=net_edge, base_size=base_size)
        if size_usdc < 1.0:
            return None

        self.log(
            f"[RESOLUTION] BUY NO @ {best_ask:.4f} | "
            f"market resolves in {hours_left:.1f}h | "
            f"yes_mid={yes_mid:.3f} (NO likely) | edge={net_edge:.3f} | "
            f"size=${size_usdc:.2f} | q={market.question[:60]}"
        )

        self._resolution_positions[market.condition_id] = best_ask

        return Signal(
            strategy="resolution",
            token_id=no_token_id,
            side="BUY",
            price=best_ask,
            size_usdc=size_usdc,
            edge=net_edge,
            notes=(
                f"[RESOLUTION] BUY NO @ {best_ask:.4f} | "
                f"market resolves in {hours_left:.1f}h | "
                f"edge={net_edge:.3f}"
            ),
            metadata={
                "strategy": "resolution",
                "outcome": "NO",
                "yes_mid": yes_mid,
                "no_mid": no_mid,
                "hours_left": hours_left,
                "gross_edge": gross_edge,
                "net_edge": net_edge,
                "condition_id": market.condition_id,
                "entered_at": time.time(),
            },
        )
