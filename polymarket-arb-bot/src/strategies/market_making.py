"""
strategies/market_making.py — Market Making

Provides liquidity by posting limit orders on both sides of the spread.
Earns the spread on each round-trip (taker pays ~0.1%, maker earns rebates).

Logic:
  1. Find markets with reasonable volume but wide spreads
  2. Post bid just above best bid, ask just below best ask
  3. Monitor inventory skew and rebalance when too one-sided
  4. Target ~0.4% spread (wider on volatile/illiquid markets)
"""
from __future__ import annotations
import os
from typing import List, Optional

from loguru import logger

from src.models import Market, MarketMakingOpportunity, Outcome


SPREAD_TARGET         = float(os.getenv("MM_SPREAD_TARGET",         "0.004"))
MAX_POSITION          = float(os.getenv("MM_MAX_POSITION_USDC",     "1000"))
REBALANCE_THRESHOLD   = float(os.getenv("MM_REBALANCE_THRESHOLD",   "0.6"))
MIN_LIQUIDITY         = 5_000    # skip illiquid markets below $5k
MIN_VOLUME_24H        = 1_000
MAX_SPREAD_TO_ENTER   = 0.05     # don't make in markets with >5% spread already


class MarketMakingStrategy:
    """
    Identifies attractive market-making opportunities.

    Best markets to make in:
      - Binary (yes/no) markets
      - Active, liquid, approaching resolution
      - Spread between best bid and best ask > our target spread
    """

    def __init__(self):
        self.inventory:        dict = {}   # market_id → {yes_shares, no_shares}
        self.opportunities_found = 0

    def scan(self, markets: List[Market]) -> List[MarketMakingOpportunity]:
        opps: List[MarketMakingOpportunity] = []

        candidates = [
            m for m in markets
            if len(m.outcomes) == 2
            and (m.liquidity or 0) >= MIN_LIQUIDITY
            and (m.volume_24h or 0) >= MIN_VOLUME_24H
        ]

        logger.debug(f"[MarketMaking] {len(candidates)} candidate markets")

        for market in candidates:
            for outcome in market.outcomes:
                opp = self._evaluate(market, outcome)
                if opp:
                    opps.append(opp)
                    self.opportunities_found += 1

        if opps:
            logger.info(f"[MarketMaking] Found {len(opps)} MM opportunities")
        return sorted(opps, key=lambda o: o.spread_pct, reverse=True)

    def _evaluate(self, market: Market, outcome: Outcome) -> Optional[MarketMakingOpportunity]:
        # Require bid/ask data (enriched via CLOB)
        bid = outcome.best_bid
        ask = outcome.best_ask
        if bid is None or ask is None:
            bid = max(0.01, outcome.price - 0.01)
            ask = min(0.99, outcome.price + 0.01)

        current_spread = ask - bid
        if current_spread < SPREAD_TARGET:
            return None   # already tight, no edge
        if current_spread > MAX_SPREAD_TO_ENTER:
            return None   # too wide = risky / illiquid

        # Our quotes: improve on best bid/ask by a tick
        tick        = 0.001
        our_bid     = round(bid + tick, 4)
        our_ask     = round(ask - tick, 4)
        our_spread  = our_ask - our_bid

        if our_spread <= 0:
            return None

        # Inventory skew (negative = long heavy, positive = short heavy)
        inv = self.inventory.get(market.market_id, {})
        yes_inv  = inv.get("yes", 0)
        no_inv   = inv.get("no", 0)
        total    = yes_inv + no_inv + 1e-8
        skew     = (yes_inv - no_inv) / total   # -1 to +1

        logger.debug(
            f"[MarketMaking] {market.question[:50]} | "
            f"{outcome.name} bid={our_bid:.3f} ask={our_ask:.3f} "
            f"spread={our_spread*100:.2f}% skew={skew:.2f}"
        )

        return MarketMakingOpportunity(
            market        = market,
            outcome       = outcome,
            bid_price     = our_bid,
            ask_price     = our_ask,
            spread_pct    = our_spread,
            inventory_skew= skew,
        )

    def update_inventory(self, market_id: str, side: str, shares: float) -> None:
        """Called by execution engine after a fill."""
        inv = self.inventory.setdefault(market_id, {"yes": 0, "no": 0})
        inv[side] += shares

    def needs_rebalance(self, market_id: str) -> bool:
        inv = self.inventory.get(market_id, {})
        yes_inv = inv.get("yes", 0)
        no_inv  = inv.get("no", 0)
        total   = yes_inv + no_inv
        if total == 0:
            return False
        skew = abs(yes_inv - no_inv) / total
        return skew > REBALANCE_THRESHOLD
