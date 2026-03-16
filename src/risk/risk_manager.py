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
            return False, "Hard stop active"

        # Drawdown check
        total = self.portfolio.total_value()
        drawdown = (self.portfolio.starting_balance - total) / self.portfolio.starting_balance
        if drawdown >= self.config.risk.max_drawdown_pct:
            self._hard_stop = True
            logger.critical(
                f"HARD STOP: drawdown {drawdown:.1%} >= limit {self.config.risk.max_drawdown_pct:.1%}"
            )
            return False, f"Drawdown limit hit: {drawdown:.1%}"

        # Balance check
        if usdc_amount > self.portfolio.usdc_balance:
            return False, f"Insufficient balance: need ${usdc_amount:.2f}"

        # Per-position size
        if usdc_amount > self.config.risk.max_position_size:
            return False, f"Position too large: ${usdc_amount:.2f} > ${self.config.risk.max_position_size:.2f}"

        # Total exposure
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
