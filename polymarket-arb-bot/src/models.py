"""
models.py — Core data structures for the Polymarket Arb Bot
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Literal
from datetime import datetime
from enum import Enum


class StrategyType(str, Enum):
    REBALANCING   = "rebalancing"
    COMBINATORIAL = "combinatorial"
    LATENCY_ARB   = "latency_arb"
    MARKET_MAKING = "market_making"


class TradeDirection(str, Enum):
    BUY  = "buy"
    SELL = "sell"


class TradeStatus(str, Enum):
    PENDING   = "pending"
    SIMULATED = "simulated"
    FILLED    = "filled"
    FAILED    = "failed"
    CANCELLED = "cancelled"


# ── Market / Outcome primitives ───────────────────────────────────────────────

class Outcome(BaseModel):
    """A single tradeable outcome within a market."""
    token_id:    str
    name:        str             # e.g. "Yes", "No", "Candidate A"
    price:       float           # 0.0–1.0
    best_bid:    Optional[float] = None
    best_ask:    Optional[float] = None
    volume_24h:  Optional[float] = None

class Market(BaseModel):
    """A Polymarket prediction market."""
    market_id:      str
    condition_id:   str
    question:       str
    category:       Optional[str] = None
    outcomes:       List[Outcome]
    end_date:       Optional[datetime] = None
    active:         bool = True
    liquidity:      Optional[float] = None
    volume_24h:     Optional[float] = None
    fetched_at:     datetime = Field(default_factory=datetime.utcnow)

    @property
    def outcome_sum(self) -> float:
        return sum(o.price for o in self.outcomes)

    @property
    def rebalancing_profit_pct(self) -> float:
        """Positive if sum < 1 (buy arb). Negative if sum > 1 (sell arb)."""
        return 1.0 - self.outcome_sum


# ── Opportunity models ────────────────────────────────────────────────────────

class RebalancingOpportunity(BaseModel):
    strategy:       StrategyType = StrategyType.REBALANCING
    market:         Market
    direction:      Literal["buy_all", "sell_all"]
    profit_pct:     float        # after estimated gas
    gross_profit:   float        # in USDC for a $100 position
    timestamp:      datetime = Field(default_factory=datetime.utcnow)


class CombinatorialOpportunity(BaseModel):
    strategy:       StrategyType = StrategyType.COMBINATORIAL
    market_a:       Market
    market_b:       Market
    relationship:   str          # human-readable description
    legs:           List[Dict]   # [{market_id, outcome, direction, price}]
    profit_pct:     float
    gross_profit:   float
    similarity:     float        # semantic similarity score
    timestamp:      datetime = Field(default_factory=datetime.utcnow)


class LatencyArbOpportunity(BaseModel):
    strategy:       StrategyType = StrategyType.LATENCY_ARB
    market:         Market
    symbol:         str          # BTC, ETH, SOL
    exchange_price: float        # real-time from Binance
    poly_implied:   float        # price implied by Polymarket odds
    lag_pct:        float        # how far Polymarket lags
    direction:      TradeDirection
    confidence:     float        # 0–1
    timestamp:      datetime = Field(default_factory=datetime.utcnow)


class MarketMakingOpportunity(BaseModel):
    strategy:       StrategyType = StrategyType.MARKET_MAKING
    market:         Market
    outcome:        Outcome
    bid_price:      float        # our bid
    ask_price:      float        # our ask
    spread_pct:     float
    inventory_skew: float        # -1 to 1
    timestamp:      datetime = Field(default_factory=datetime.utcnow)


# ── Trade models ──────────────────────────────────────────────────────────────

class Trade(BaseModel):
    trade_id:       str
    strategy:       StrategyType
    market_id:      str
    outcome_name:   str
    token_id:       str
    direction:      TradeDirection
    size_usdc:      float
    limit_price:    float
    filled_price:   Optional[float] = None
    filled_size:    Optional[float] = None
    profit_usdc:    Optional[float] = None
    status:         TradeStatus = TradeStatus.PENDING
    opened_at:      datetime = Field(default_factory=datetime.utcnow)
    closed_at:      Optional[datetime] = None
    notes:          Optional[str] = None


class Portfolio(BaseModel):
    """Paper trading portfolio state."""
    cash_usdc:          float
    starting_cash:      float
    open_trades:        Dict[str, Trade] = {}
    closed_trades:      List[Trade] = []
    total_pnl:          float = 0.0
    realized_pnl:       float = 0.0
    unrealized_pnl:     float = 0.0
    total_trades:       int = 0
    winning_trades:     int = 0
    losing_trades:      int = 0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def total_value(self) -> float:
        return self.cash_usdc + self.unrealized_pnl

    @property
    def return_pct(self) -> float:
        return (self.total_value - self.starting_cash) / self.starting_cash * 100
