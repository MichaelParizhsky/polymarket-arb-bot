"""
Performance analysis utilities.
Computes per-strategy metrics from trade history for the meta-agent.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StrategyMetrics:
    name: str
    total_trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    total_pnl: float = 0.0
    total_volume: float = 0.0
    total_fees: float = 0.0
    avg_trade_size: float = 0.0
    win_rate: float = 0.0          # % of sell trades that were profitable
    avg_edge_captured: float = 0.0
    trades_per_hour: float = 0.0
    first_trade_ts: float = 0.0
    last_trade_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total_trades": self.total_trades,
            "total_pnl_usdc": round(self.total_pnl, 4),
            "total_volume_usdc": round(self.total_volume, 2),
            "total_fees_usdc": round(self.total_fees, 4),
            "avg_trade_size_usdc": round(self.avg_trade_size, 2),
            "win_rate_pct": round(self.win_rate * 100, 1),
            "trades_per_hour": round(self.trades_per_hour, 2),
        }


@dataclass
class PortfolioSnapshot:
    snapshot_time: float
    starting_balance: float
    usdc_balance: float
    total_value: float
    total_pnl: float
    fees_paid: float
    open_positions: int
    strategy_pnl: dict[str, float]
    trades: list[dict]
    strategy_metrics: dict[str, StrategyMetrics] = field(default_factory=dict)

    @classmethod
    def from_json(cls, path: str = "logs/portfolio_state.json") -> "PortfolioSnapshot":
        with open(path) as f:
            data = json.load(f)
        snap = cls(
            snapshot_time=data["snapshot_time"],
            starting_balance=data["starting_balance"],
            usdc_balance=data["usdc_balance"],
            total_value=data["total_value"],
            total_pnl=data["total_pnl"],
            fees_paid=data["fees_paid"],
            open_positions=data["open_positions"],
            strategy_pnl=data["strategy_pnl"],
            trades=data["trades"],
        )
        snap.strategy_metrics = snap._compute_metrics()
        return snap

    def _compute_metrics(self) -> dict[str, StrategyMetrics]:
        metrics: dict[str, StrategyMetrics] = {}

        for t in self.trades:
            name = t["strategy"]
            if name not in metrics:
                metrics[name] = StrategyMetrics(name=name)
            m = metrics[name]
            m.total_trades += 1
            m.total_volume += t["usdc_amount"]
            m.total_fees += t["fee"]

            if t["side"] == "BUY":
                m.buy_trades += 1
                m.total_pnl -= t["usdc_amount"] + t["fee"]
            else:
                m.sell_trades += 1
                m.total_pnl += t["usdc_amount"] - t["fee"]

            if m.first_trade_ts == 0:
                m.first_trade_ts = t["timestamp"]
            m.last_trade_ts = t["timestamp"]

        for m in metrics.values():
            if m.total_trades > 0:
                m.avg_trade_size = m.total_volume / m.total_trades
            hours = max((m.last_trade_ts - m.first_trade_ts) / 3600, 0.01)
            m.trades_per_hour = m.total_trades / hours

        return metrics

    def age_hours(self) -> float:
        return (time.time() - self.snapshot_time) / 3600

    def to_analysis_dict(self) -> dict:
        """Structured summary for Claude to analyze."""
        return {
            "portfolio": {
                "starting_balance": self.starting_balance,
                "current_value": round(self.total_value, 2),
                "total_pnl_usdc": round(self.total_pnl, 2),
                "total_pnl_pct": round((self.total_pnl / self.starting_balance) * 100, 3),
                "fees_paid": round(self.fees_paid, 2),
                "open_positions": self.open_positions,
                "total_trades": len(self.trades),
                "data_age_hours": round(self.age_hours(), 1),
            },
            "strategies": {
                name: m.to_dict()
                for name, m in self.strategy_metrics.items()
            },
            "strategy_pnl": {
                k: round(v, 2) for k, v in self.strategy_pnl.items()
            },
        }
