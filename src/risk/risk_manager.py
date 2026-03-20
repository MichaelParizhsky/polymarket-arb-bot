"""
Risk management: position sizing, drawdown protection, exposure limits.
"""
from __future__ import annotations

import time

from src.utils.logger import logger


class RiskManager:
    def __init__(self, config, portfolio) -> None:
        self.config = config
        self.portfolio = portfolio
        self._hard_stop = False
        self._permanent_lock = False
        self._hard_stop_count: int = 0
        self._hard_stop_timestamps: list[float] = []
        self._strategy_pnl: dict[str, float] = {
            strategy: 0.0
            for strategy in config.risk.strategy_loss_budget
        }
        self._category_exposure: dict[str, float] = {}

    def check_trade(
        self,
        token_id: str,
        side: str,
        usdc_amount: float,
        strategy: str,
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Validates a proposed trade against risk limits.
        """
        # Permanent lock — requires manual reset via reset_permanent_lock()
        if self._permanent_lock:
            return False, "Permanent lock active — manual reset required"

        if self._hard_stop:
            return False, "Hard stop active"

        # Drawdown check applies to all trade directions
        total = self.portfolio.total_value()
        drawdown = (self.portfolio.starting_balance - total) / self.portfolio.starting_balance
        if drawdown >= self.config.risk.max_drawdown_pct:
            self._hard_stop = True
            now = time.time()
            self._hard_stop_timestamps.append(now)
            window_seconds = self.config.risk.hard_stop_window_hours * 3600
            self._hard_stop_timestamps = [
                ts for ts in self._hard_stop_timestamps
                if now - ts <= window_seconds
            ]
            self._hard_stop_count = len(self._hard_stop_timestamps)
            logger.critical(
                f"HARD STOP: drawdown {drawdown:.1%} >= limit {self.config.risk.max_drawdown_pct:.1%} "
                f"(stop #{self._hard_stop_count} in {self.config.risk.hard_stop_window_hours}h window)"
            )
            if self._hard_stop_count >= self.config.risk.hard_stop_max_count:
                self._permanent_lock = True
                logger.critical(
                    f"PERMANENT LOCK: {self._hard_stop_count} hard stops within "
                    f"{self.config.risk.hard_stop_window_hours}h — manual reset required via reset_permanent_lock()"
                )
            return False, f"Drawdown limit hit: {drawdown:.1%}"

        # SELL trades liquidate positions and return USDC — skip buy-side checks
        if side.upper() == "SELL":
            position = self.portfolio.positions.get(token_id)
            if position is None or position.contracts <= 0:
                return False, f"No position to sell: {token_id}"
            return True, "OK"

        # Strategy loss budget check (BUY only)
        strategy_loss = abs(min(0.0, self._strategy_pnl.get(strategy, 0.0)))
        budget = self.config.risk.strategy_loss_budget.get(strategy, float("inf"))
        if strategy_loss >= budget:
            logger.warning(
                f"Strategy loss budget exhausted: {strategy} — loss ${strategy_loss:.2f} >= budget ${budget:.2f}"
            )
            return False, f"Strategy loss budget exhausted: {strategy}"

        # Balance check (BUY only)
        if usdc_amount > self.portfolio.usdc_balance:
            return False, f"Insufficient balance: need ${usdc_amount:.2f}"

        # Per-position size (BUY only)
        if usdc_amount > self.config.risk.max_position_size:
            return False, f"Position too large: ${usdc_amount:.2f} > ${self.config.risk.max_position_size:.2f}"

        # Total exposure (BUY only — sells reduce exposure)
        current_exposure = self.portfolio.exposure()
        if current_exposure + usdc_amount > self.config.risk.max_total_exposure:
            return False, (
                f"Exposure limit: ${current_exposure+usdc_amount:.2f} > "
                f"${self.config.risk.max_total_exposure:.2f}"
            )

        # Open order count
        if len(self.portfolio.open_orders) >= self.config.risk.max_open_orders:
            return False, f"Too many open orders: {len(self.portfolio.open_orders)}"

        return True, "OK"

    def size_position(self, edge: float, base_size: float | None = None) -> float:
        """
        Kelly-inspired position sizing.
        Returns USDC amount to risk on a given edge.
        """
        base = base_size or self.config.risk.max_position_size
        # Scale by edge / threshold ratio, capped at 100%
        scale = min(edge / max(self.config.risk.min_edge_threshold, 0.001), 1.0)
        # Also scale by available capital
        available = min(
            self.portfolio.usdc_balance,
            self.config.risk.max_total_exposure - self.portfolio.exposure(),
        )
        raw_size = base * scale
        return min(raw_size, available, self.config.risk.max_position_size)

    def is_hard_stopped(self) -> bool:
        return self._hard_stop or self._permanent_lock

    def reset_hard_stop(self) -> None:
        """Manual override — use carefully."""
        self._hard_stop = False
        logger.warning("Hard stop reset manually")

    def reset_permanent_lock(self) -> None:
        """Clear the permanent lock — requires explicit human intervention."""
        self._permanent_lock = False
        self._hard_stop = False
        self._hard_stop_timestamps.clear()
        self._hard_stop_count = 0
        logger.warning("Permanent lock reset manually — trading re-enabled")

    def record_trade_result(self, strategy: str, pnl: float) -> None:
        """Update per-strategy PnL tracking."""
        self._strategy_pnl[strategy] = self._strategy_pnl.get(strategy, 0.0) + pnl

    def check_orderbook_depth(
        self, orderbook, side: str, required_usdc: float
    ) -> tuple[bool, str]:
        """
        Returns (ok, reason).
        For a BUY, checks the top 5 ask levels can absorb required_usdc * 1.5.
        """
        levels = orderbook.asks if side.upper() == "BUY" else orderbook.bids
        available_usdc = sum(level.price * level.size for level in levels[:5])
        needed = required_usdc * 1.5
        if available_usdc >= needed:
            return True, "ok"
        return (
            False,
            f"Insufficient depth: {available_usdc:.1f} USDC available, need {needed:.1f}",
        )

    def check_correlation(
        self, category: str, usdc_amount: float
    ) -> tuple[bool, str]:
        """
        Returns (ok, reason).
        Rejects trade if a single category would exceed 40% of total exposure.
        """
        total_exposure = sum(self._category_exposure.values())
        current_category = self._category_exposure.get(category, 0.0)
        new_category = current_category + usdc_amount
        denominator = max(total_exposure, 1.0)
        if new_category / denominator > 0.40:
            return (
                False,
                f"Correlation limit: category '{category}' would be "
                f"{new_category / denominator:.1%} of exposure (limit 40%)",
            )
        return True, "ok"

    def record_category_exposure(self, category: str, delta: float) -> None:
        """Update category exposure when a trade executes or closes."""
        self._category_exposure[category] = (
            self._category_exposure.get(category, 0.0) + delta
        )

    def portfolio_health_score(self) -> dict:
        """
        Fast health check on current portfolio state.
        Returns a score (0-100) and flags for the meta-agent / dashboard.

        Dimensions:
          - Capital safety: how close to hard stop (drawdown limit)
          - Concentration: single-position exposure vs total exposure
          - Liquidity: free USDC as % of total value
          - Activity: are we trading (not stalled)?
        """
        total = self.portfolio.total_value()
        if total <= 0:
            return {"score": 0, "grade": "CRITICAL", "flags": ["zero portfolio value"]}

        drawdown = max(
            0.0,
            (self.portfolio.starting_balance - total) / self.portfolio.starting_balance,
        )
        dd_limit = self.config.risk.max_drawdown_pct
        dd_ratio = drawdown / dd_limit if dd_limit > 0 else 1.0  # 0=safe, 1=at limit

        exposure = self.portfolio.exposure()
        max_exposure = self.config.risk.max_total_exposure
        exposure_ratio = exposure / max_exposure if max_exposure > 0 else 1.0

        free_usdc_pct = (self.portfolio.usdc_balance / total) * 100

        # Concentration: largest single position as % of total exposure
        positions = self.portfolio.positions
        if positions and exposure > 0:
            largest = max(p.cost_basis for p in positions.values())
            concentration_pct = (largest / exposure) * 100
        else:
            concentration_pct = 0.0

        flags = []

        # Capital safety (40 pts)
        capital_score = max(0.0, 40.0 * (1.0 - dd_ratio))
        if dd_ratio > 0.7:
            flags.append(f"drawdown at {drawdown:.1%} — approaching hard stop ({dd_limit:.0%})")

        # Exposure headroom (25 pts)
        exposure_score = max(0.0, 25.0 * (1.0 - exposure_ratio))
        if exposure_ratio > 0.85:
            flags.append(f"exposure at {exposure_ratio:.0%} of limit — limited capacity for new trades")

        # Liquidity (20 pts): >30% free USDC = full points
        liquidity_score = min(20.0, (free_usdc_pct / 30.0) * 20.0)
        if free_usdc_pct < 10.0:
            flags.append(f"only {free_usdc_pct:.1f}% capital free — capital locked in positions")

        # Concentration (15 pts): <25% in single position = full points
        concentration_score = max(0.0, 15.0 * (1.0 - max(0.0, concentration_pct - 25.0) / 75.0))
        if concentration_pct > 50.0:
            flags.append(f"concentration risk: largest position is {concentration_pct:.0f}% of exposure")

        total_score = round(capital_score + exposure_score + liquidity_score + concentration_score, 1)
        grade = "HEALTHY" if total_score >= 75 else "FAIR" if total_score >= 50 else "WEAK" if total_score >= 25 else "CRITICAL"

        if self._permanent_lock:
            total_score = 0.0
            grade = "CRITICAL"
            flags.insert(0, "PERMANENT LOCK ACTIVE — manual reset required")
        elif self._hard_stop:
            total_score = 0.0
            grade = "CRITICAL"
            flags.insert(0, "HARD STOP ACTIVE")

        return {
            "score": total_score,
            "grade": grade,
            "flags": flags,
            "drawdown_pct": round(drawdown * 100, 2),
            "exposure_ratio_pct": round(exposure_ratio * 100, 1),
            "free_usdc_pct": round(free_usdc_pct, 1),
            "concentration_pct": round(concentration_pct, 1),
        }
