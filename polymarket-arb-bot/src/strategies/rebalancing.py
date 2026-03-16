"""
strategies/rebalancing.py — Market Rebalancing Arbitrage

Logic: In any Polymarket NegRisk market, all outcome prices must sum to exactly $1.
When sum < $1, buying one share of every outcome guarantees a $1 payout at resolution.
When sum > $1, selling all outcomes (or minting + selling) captures the excess.
"""
from __future__ import annotations
import os
from typing import List, Optional

from loguru import logger

from src.models import Market, RebalancingOpportunity, StrategyType


MIN_PROFIT_PCT = float(os.getenv("REBALANCING_MIN_PROFIT_PCT", "0.003"))
GAS_COST       = float(os.getenv("GAS_COST_ESTIMATE_USDC", "0.02"))
MAX_POSITION   = float(os.getenv("REBALANCING_MAX_POSITION_USDC", "500"))


class RebalancingStrategy:
    """
    Scans a list of markets for rebalancing arbitrage.

    For a $100 position:
      - Buy arb:  cost = sum_of_prices * 100, payout = $100
      - profit   = (1 - outcome_sum) * 100

    After estimating gas for N outcome legs:
      net_profit = gross_profit - (N * GAS_COST)
    """

    def __init__(self):
        self.opportunities_found = 0
        self.opportunities_taken = 0

    def scan(self, markets: List[Market]) -> List[RebalancingOpportunity]:
        """Return all markets with a profitable rebalancing opportunity."""
        opps: List[RebalancingOpportunity] = []

        for market in markets:
            opp = self._evaluate(market)
            if opp:
                opps.append(opp)
                self.opportunities_found += 1

        if opps:
            logger.info(f"[Rebalancing] Found {len(opps)} opportunities")
        return sorted(opps, key=lambda o: o.profit_pct, reverse=True)

    def _evaluate(self, market: Market) -> Optional[RebalancingOpportunity]:
        if len(market.outcomes) < 2:
            return None

        total = market.outcome_sum
        n_legs = len(market.outcomes)

        # ── Buy arb: sum < 1 ──────────────────────────────────────────────────
        if total < 1.0:
            gas_total    = n_legs * GAS_COST
            gross_profit = (1.0 - total) * MAX_POSITION
            net_profit   = gross_profit - gas_total
            profit_pct   = net_profit / (total * MAX_POSITION)

            if profit_pct < MIN_PROFIT_PCT:
                return None

            logger.debug(
                f"[Rebalancing] BUY ARB | {market.question[:60]} | "
                f"sum={total:.4f} | profit={profit_pct*100:.3f}%"
            )
            return RebalancingOpportunity(
                market      = market,
                direction   = "buy_all",
                profit_pct  = profit_pct,
                gross_profit= net_profit,
            )

        # ── Sell arb: sum > 1 ────────────────────────────────────────────────
        # Mint a complete set for $1 then sell all YES outcomes above $1 total
        if total > 1.0:
            gas_total    = (n_legs + 1) * GAS_COST   # +1 for mint tx
            gross_profit = (total - 1.0) * MAX_POSITION
            net_profit   = gross_profit - gas_total
            profit_pct   = net_profit / MAX_POSITION

            if profit_pct < MIN_PROFIT_PCT:
                return None

            logger.debug(
                f"[Rebalancing] SELL ARB | {market.question[:60]} | "
                f"sum={total:.4f} | profit={profit_pct*100:.3f}%"
            )
            return RebalancingOpportunity(
                market      = market,
                direction   = "sell_all",
                profit_pct  = profit_pct,
                gross_profit= net_profit,
            )

        return None
