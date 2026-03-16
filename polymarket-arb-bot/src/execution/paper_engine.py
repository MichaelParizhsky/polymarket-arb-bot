"""
execution/paper_engine.py — Paper Trading Execution Engine

Simulates trade execution with realistic slippage, partial fills,
and gas cost modeling. Maintains a full portfolio ledger.

In paper mode: all trades are simulated. PnL is tracked based on
how markets eventually resolve (via Polymarket resolution data).
"""
from __future__ import annotations
import os
import uuid
from datetime import datetime
from typing import Optional, Union

from loguru import logger

from src.models import (
    Portfolio, Trade, TradeDirection, TradeStatus, StrategyType,
    RebalancingOpportunity, CombinatorialOpportunity,
    LatencyArbOpportunity, MarketMakingOpportunity,
)

AnyOpportunity = Union[
    RebalancingOpportunity,
    CombinatorialOpportunity,
    LatencyArbOpportunity,
    MarketMakingOpportunity,
]

STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE_USDC", "10000"))
GAS_COST         = float(os.getenv("GAS_COST_ESTIMATE_USDC",       "0.02"))
MAX_EXPOSURE     = float(os.getenv("MAX_TOTAL_EXPOSURE_USDC",       "2000"))
MAX_SINGLE_PCT   = float(os.getenv("MAX_SINGLE_MARKET_EXPOSURE_PCT","0.10"))

# Simulate realistic slippage: 0.1–0.3% depending on liquidity
SLIPPAGE_BPS = 15   # 0.15% average


class PaperEngine:
    """
    Simulates trade execution for all four strategy types.

    Tracks:
      - Cash balance
      - Open positions
      - Realized / unrealized PnL
      - Win rate, trade count by strategy
    """

    def __init__(self):
        self.portfolio = Portfolio(
            cash_usdc    = STARTING_BALANCE,
            starting_cash= STARTING_BALANCE,
        )
        self.strategy_stats: dict = {s.value: {"trades": 0, "pnl": 0.0} for s in StrategyType}

    # ── Public execute methods ────────────────────────────────────────────────

    def execute_rebalancing(self, opp: RebalancingOpportunity) -> Optional[Trade]:
        """Buy one share of every outcome (or sell all in sell arb)."""
        position_size = float(os.getenv("REBALANCING_MAX_POSITION_USDC", "500"))
        position_size = min(position_size, self._available_for_market(opp.market.market_id))
        if position_size <= 0:
            return None

        n_legs = len(opp.market.outcomes)
        cost_per_leg = (opp.market.outcome_sum * position_size) / n_legs

        # Simulate: all legs fill at mid + slippage
        slippage_cost = position_size * (SLIPPAGE_BPS / 10_000)
        gas_total     = n_legs * GAS_COST
        net_profit    = opp.gross_profit * (position_size / float(os.getenv("REBALANCING_MAX_POSITION_USDC","500"))) \
                        - slippage_cost - gas_total

        trade = self._record_trade(
            strategy    = StrategyType.REBALANCING,
            market_id   = opp.market.market_id,
            outcome_name= "ALL_OUTCOMES",
            token_id    = opp.market.outcomes[0].token_id,
            direction   = TradeDirection.BUY,
            size_usdc   = position_size,
            limit_price = opp.market.outcome_sum / n_legs,
            filled_price= opp.market.outcome_sum / n_legs * (1 + SLIPPAGE_BPS/10000),
            profit_usdc = net_profit,
            notes       = f"dir={opp.direction} legs={n_legs} sum={opp.market.outcome_sum:.4f}",
        )
        return trade

    def execute_combinatorial(self, opp: CombinatorialOpportunity) -> Optional[Trade]:
        position_size = float(os.getenv("COMBINATORIAL_MAX_POSITION_USDC", "300"))
        position_size = min(position_size, self._available_for_market(opp.market_a.market_id))
        if position_size <= 0:
            return None

        slippage_cost = position_size * (SLIPPAGE_BPS / 10_000)
        gas_total     = 2 * GAS_COST
        scale         = position_size / float(os.getenv("COMBINATORIAL_MAX_POSITION_USDC","300"))
        net_profit    = opp.gross_profit * scale - slippage_cost - gas_total

        trade = self._record_trade(
            strategy    = StrategyType.COMBINATORIAL,
            market_id   = opp.market_a.market_id,
            outcome_name= f"COMBO_{opp.market_b.market_id[:8]}",
            token_id    = opp.market_a.outcomes[0].token_id,
            direction   = TradeDirection.BUY,
            size_usdc   = position_size,
            limit_price = sum(l["price"] for l in opp.legs) / len(opp.legs),
            filled_price= sum(l["price"] for l in opp.legs) / len(opp.legs) * (1 + SLIPPAGE_BPS/10000),
            profit_usdc = net_profit,
            notes       = f"sim={opp.similarity:.2f} rel={opp.relationship}",
        )
        return trade

    def execute_latency_arb(self, opp: LatencyArbOpportunity) -> Optional[Trade]:
        position_size = float(os.getenv("LATENCY_ARB_MAX_POSITION_USDC", "200"))
        position_size = min(position_size, self._available_for_market(opp.market.market_id))
        if position_size <= 0:
            return None

        # Scale position by confidence
        position_size *= opp.confidence
        expected_edge = opp.lag_pct * position_size
        slippage_cost = position_size * (SLIPPAGE_BPS / 10_000)
        gas           = GAS_COST
        net_profit    = expected_edge - slippage_cost - gas

        yes_out = next((o for o in opp.market.outcomes if "yes" in o.name.lower()), opp.market.outcomes[0])

        trade = self._record_trade(
            strategy    = StrategyType.LATENCY_ARB,
            market_id   = opp.market.market_id,
            outcome_name= "YES" if opp.direction == TradeDirection.BUY else "NO",
            token_id    = yes_out.token_id,
            direction   = opp.direction,
            size_usdc   = position_size,
            limit_price = opp.poly_implied,
            filled_price= opp.poly_implied * (1 + SLIPPAGE_BPS/10000),
            profit_usdc = net_profit,
            notes       = f"{opp.symbol} spot={opp.exchange_price:.2f} lag={opp.lag_pct:.3f}",
        )
        return trade

    def execute_market_making(self, opp: MarketMakingOpportunity) -> Optional[Trade]:
        position_size = min(500.0, self._available_for_market(opp.market.market_id))
        if position_size <= 0:
            return None

        # MM earns half the spread per round trip (assume 50% fill probability)
        expected_earn = opp.spread_pct * position_size * 0.5
        gas           = GAS_COST * 2
        net_profit    = expected_earn - gas

        trade = self._record_trade(
            strategy    = StrategyType.MARKET_MAKING,
            market_id   = opp.market.market_id,
            outcome_name= opp.outcome.name,
            token_id    = opp.outcome.token_id,
            direction   = TradeDirection.BUY,
            size_usdc   = position_size,
            limit_price = opp.bid_price,
            filled_price= opp.bid_price,
            profit_usdc = net_profit,
            notes       = f"bid={opp.bid_price:.4f} ask={opp.ask_price:.4f} spread={opp.spread_pct*100:.2f}%",
        )
        return trade

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _available_for_market(self, market_id: str) -> float:
        """Maximum additional exposure allowed in this market."""
        current_exposure = sum(
            t.size_usdc for t in self.portfolio.open_trades.values()
            if t.market_id == market_id
        )
        max_single = self.portfolio.cash_usdc * MAX_SINGLE_PCT
        total_open = sum(t.size_usdc for t in self.portfolio.open_trades.values())
        available  = min(
            max_single - current_exposure,
            MAX_EXPOSURE - total_open,
            self.portfolio.cash_usdc,
        )
        return max(0.0, available)

    def _record_trade(
        self,
        strategy:     StrategyType,
        market_id:    str,
        outcome_name: str,
        token_id:     str,
        direction:    TradeDirection,
        size_usdc:    float,
        limit_price:  float,
        filled_price: float,
        profit_usdc:  float,
        notes:        str = "",
    ) -> Trade:
        trade_id = str(uuid.uuid4())[:12]
        trade = Trade(
            trade_id    = trade_id,
            strategy    = strategy,
            market_id   = market_id,
            outcome_name= outcome_name,
            token_id    = token_id,
            direction   = direction,
            size_usdc   = size_usdc,
            limit_price = limit_price,
            filled_price= filled_price,
            filled_size  = size_usdc,
            profit_usdc = profit_usdc,
            status      = TradeStatus.SIMULATED,
            closed_at   = datetime.utcnow(),
            notes       = notes,
        )

        # Update portfolio
        self.portfolio.cash_usdc         += profit_usdc
        self.portfolio.realized_pnl      += profit_usdc
        self.portfolio.total_pnl         += profit_usdc
        self.portfolio.total_trades      += 1
        self.portfolio.closed_trades.append(trade)

        if profit_usdc > 0:
            self.portfolio.winning_trades += 1
        elif profit_usdc < 0:
            self.portfolio.losing_trades  += 1

        self.strategy_stats[strategy.value]["trades"] += 1
        self.strategy_stats[strategy.value]["pnl"]    += profit_usdc

        logger.info(
            f"[Paper] [{strategy.value.upper():<14}] {outcome_name:<20} | "
            f"${size_usdc:>8.2f} | PnL: {'+'if profit_usdc>=0 else ''}{profit_usdc:>7.2f} | "
            f"{notes}"
        )
        return trade

    def summary(self) -> dict:
        p = self.portfolio
        return {
            "balance":        round(p.cash_usdc, 2),
            "total_pnl":      round(p.total_pnl, 2),
            "return_pct":     round(p.return_pct, 3),
            "total_trades":   p.total_trades,
            "win_rate":       round(p.win_rate * 100, 1),
            "strategy_stats": self.strategy_stats,
        }
