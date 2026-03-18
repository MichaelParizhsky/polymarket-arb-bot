"""
Risk management: position sizing, drawdown protection, exposure limits.
"""
from __future__ import annotations

from src.utils.logger import logger


class RiskManager:
    def __init__(self, config, portfolio) -> None:
        self.config = config
        self.portfolio = portfolio
        self._hard_stop = False

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
        if self._hard_stop:
            total = self.portfolio.total_value()
            recovery_threshold = self.portfolio.starting_balance * (1 - self.config.risk.max_drawdown_pct * 0.5)
            if total >= recovery_threshold:
                self._hard_stop = False
                logger.warning(
                    f"Hard stop auto-reset: portfolio ${total:.2f} recovered above threshold ${recovery_threshold:.2f}"
                )
            else:
                return False, "Hard stop active"

        # Drawdown check applies to all trade directions
        total = self.portfolio.total_value()
        drawdown = (self.portfolio.starting_balance - total) / self.portfolio.starting_balance
        if drawdown >= self.config.risk.max_drawdown_pct:
            self._hard_stop = True
            logger.critical(
                f"HARD STOP: drawdown {drawdown:.1%} >= limit {self.config.risk.max_drawdown_pct:.1%}"
            )
            return False, f"Drawdown limit hit: {drawdown:.1%}"

        # SELL trades liquidate positions and return USDC — skip buy-side checks
        if side.upper() == "SELL":
            position = self.portfolio.positions.get(token_id)
            if position is None or position.contracts <= 0:
                return False, f"No position to sell: {token_id}"
            return True, "OK"

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
        return self._hard_stop

    def reset_hard_stop(self) -> None:
        """Manual override — use carefully."""
        self._hard_stop = False
        logger.warning("Hard stop reset manually")

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

        if self._hard_stop:
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
