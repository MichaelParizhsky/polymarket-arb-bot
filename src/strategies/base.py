"""Base class for all arbitrage strategies."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.utils.logger import logger


@dataclass
class Signal:
    strategy: str
    token_id: str
    side: str              # "BUY" or "SELL"
    price: float
    size_usdc: float
    edge: float            # Expected profit in USDC per dollar risked
    notes: str = ""
    metadata: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    """
    Every strategy must implement scan() which returns a list of Signals.
    The bot loop calls scan() on each active strategy every cycle.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        self.config = config
        self.portfolio = portfolio
        self.risk = risk_manager
        self.name = self.__class__.__name__

    @abstractmethod
    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        """
        Scan for arbitrage opportunities.
        context: shared data (markets, orderbooks, binance prices, etc.)
        Returns list of actionable signals.
        """
        ...

    def log(self, msg: str, level: str = "info") -> None:
        getattr(logger, level)(f"[{self.name}] {msg}")
