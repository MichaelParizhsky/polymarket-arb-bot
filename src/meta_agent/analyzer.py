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
    closed_positions: list[dict] = field(default_factory=list)
    portfolio_win_rate: float = 0.0
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
            closed_positions=data.get("closed_positions", []),
            portfolio_win_rate=data.get("win_rate", 0.0),
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

        # Compute per-strategy win rate from closed_positions
        strategy_wins: dict[str, int] = {}
        strategy_closes: dict[str, int] = {}
        for cp in self.closed_positions:
            strat = cp.get("strategy", "unknown")
            strategy_closes[strat] = strategy_closes.get(strat, 0) + 1
            if cp.get("realized_pnl", 0.0) > 0:
                strategy_wins[strat] = strategy_wins.get(strat, 0) + 1

        for m in metrics.values():
            if m.total_trades > 0:
                m.avg_trade_size = m.total_volume / m.total_trades
            hours = max((m.last_trade_ts - m.first_trade_ts) / 3600, 0.01)
            m.trades_per_hour = m.total_trades / hours
            closes = strategy_closes.get(m.name, 0)
            if closes > 0:
                m.win_rate = strategy_wins.get(m.name, 0) / closes

        return metrics

    def age_hours(self) -> float:
        return (time.time() - self.snapshot_time) / 3600

    def _compute_health_score(self) -> dict:
        """
        Score portfolio health 0-100 using four dimensions:
          - Profitability (40 pts): PnL % vs starting balance
          - Fee efficiency (25 pts): fees as % of gross volume
          - Win rate (25 pts): % of closed positions profitable
          - Drawdown safety (10 pts): inverse of drawdown severity

        Returns score + per-dimension breakdown for meta-agent context.
        """
        total_volume = sum(t["usdc_amount"] for t in self.trades)
        fee_drag_pct = (self.fees_paid / total_volume * 100) if total_volume > 0 else 0.0

        pnl_pct = (self.total_pnl / self.starting_balance) * 100 if self.starting_balance else 0.0
        drawdown_pct = max(0.0, -pnl_pct)

        # Win rate: use portfolio_win_rate from closed positions (most accurate)
        # Fall back to per-strategy average if portfolio-level not available
        if self.portfolio_win_rate > 0.0:
            win_rate_pct = self.portfolio_win_rate
        elif self.closed_positions:
            wins = sum(1 for cp in self.closed_positions if cp.get("realized_pnl", 0.0) > 0)
            win_rate_pct = wins / len(self.closed_positions) * 100
        else:
            win_rates = [m.win_rate * 100 for m in self.strategy_metrics.values() if m.total_trades > 0]
            win_rate_pct = sum(win_rates) / len(win_rates) if win_rates else 0.0

        # Profitability score (40 pts): 0% = 0pts, +2% = 40pts
        profit_score = min(40.0, max(0.0, pnl_pct * 20))

        # Fee efficiency score (25 pts): <0.3% drag = 25pts, >2% drag = 0pts
        fee_score = max(0.0, 25.0 - (fee_drag_pct / 2.0) * 25.0)

        # Win rate score (25 pts): 50% = 25pts, <40% = 0pts
        win_score = max(0.0, min(25.0, (win_rate_pct - 40.0) * 2.5))

        # Drawdown safety score (10 pts): 0% DD = 10pts, 15%+ DD = 0pts
        dd_score = max(0.0, 10.0 - (drawdown_pct / 15.0) * 10.0)

        total_score = round(profit_score + fee_score + win_score + dd_score, 1)

        # Bootstrap phase: fewer than 30 closed positions means stats are unreliable.
        # Inflate score to FAIR minimum so meta-agent doesn't over-react to noise.
        bootstrap = len(self.closed_positions) < 30
        if bootstrap and total_score < 50:
            total_score = 50.0

        if total_score >= 75:
            grade = "HEALTHY"
        elif total_score >= 50:
            grade = "FAIR"
        elif total_score >= 25:
            grade = "WEAK"
        else:
            grade = "CRITICAL"

        return {
            "score": total_score,
            "grade": grade,
            "breakdown": {
                "profitability": round(profit_score, 1),
                "fee_efficiency": round(fee_score, 1),
                "win_rate": round(win_score, 1),
                "drawdown_safety": round(dd_score, 1),
            },
            "fee_drag_pct": round(fee_drag_pct, 3),
            "drawdown_pct": round(drawdown_pct, 2),
            "win_rate_pct": round(win_rate_pct, 1),
            "closed_positions": len(self.closed_positions),
            "bootstrap_phase": bootstrap,
        }

    def to_analysis_dict(self) -> dict:
        """Structured summary for Claude to analyze."""
        total_volume = sum(t["usdc_amount"] for t in self.trades)
        fee_drag_pct = (self.fees_paid / total_volume * 100) if total_volume > 0 else 0.0

        # Hourly PnL rate from trade timestamps
        if len(self.trades) >= 2:
            span_hours = max(
                (self.trades[-1]["timestamp"] - self.trades[0]["timestamp"]) / 3600, 0.01
            )
            hourly_pnl = self.total_pnl / span_hours
        else:
            hourly_pnl = 0.0

        # Per-strategy ROI: PnL / volume traded
        strategy_roi: dict[str, float] = {}
        strategy_volume: dict[str, float] = {}
        for t in self.trades:
            strategy_volume[t["strategy"]] = (
                strategy_volume.get(t["strategy"], 0.0) + t["usdc_amount"]
            )
        for name, pnl in self.strategy_pnl.items():
            vol = strategy_volume.get(name, 0.0)
            strategy_roi[name] = round((pnl / vol * 100) if vol > 0 else 0.0, 3)

        health = self._compute_health_score()

        # Best / worst strategy by ROI (min 5 trades)
        qualified = {
            name: roi for name, roi in strategy_roi.items()
            if self.strategy_metrics.get(name, StrategyMetrics(name)).total_trades >= 5
        }
        best_strategy = max(qualified, key=qualified.get) if qualified else None
        worst_strategy = min(qualified, key=qualified.get) if qualified else None

        return {
            "portfolio": {
                "starting_balance": self.starting_balance,
                "current_value": round(self.total_value, 2),
                "total_pnl_usdc": round(self.total_pnl, 2),
                "total_pnl_pct": round((self.total_pnl / self.starting_balance) * 100, 3),
                "fees_paid": round(self.fees_paid, 2),
                "fee_drag_pct": round(fee_drag_pct, 3),
                "hourly_pnl_usdc": round(hourly_pnl, 4),
                "open_positions": self.open_positions,
                "total_trades": len(self.trades),
                "data_age_hours": round(self.age_hours(), 1),
            },
            "health": health,
            "strategies": {
                name: m.to_dict()
                for name, m in self.strategy_metrics.items()
            },
            "strategy_pnl": {
                k: round(v, 2) for k, v in self.strategy_pnl.items()
            },
            "strategy_roi_pct": strategy_roi,
            "best_strategy": best_strategy,
            "worst_strategy": worst_strategy,
        }
