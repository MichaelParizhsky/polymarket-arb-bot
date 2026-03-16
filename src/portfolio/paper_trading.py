"""
Paper trading engine.
Tracks virtual positions, P&L, and order history.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from src.utils.logger import logger
from src.utils.metrics import (
    portfolio_balance, portfolio_pnl, open_positions, total_exposure
)


@dataclass
class Position:
    token_id: str
    market_question: str
    outcome: str          # "Yes" or "No"
    contracts: float      # number of outcome tokens held
    avg_cost: float       # average cost per token in USDC
    strategy: str
    opened_at: float = field(default_factory=time.time)
    realized_pnl: float = 0.0

    @property
    def cost_basis(self) -> float:
        return self.contracts * self.avg_cost

    def unrealized_pnl(self, current_price: float) -> float:
        return self.contracts * (current_price - self.avg_cost)


@dataclass
class Trade:
    trade_id: str
    strategy: str
    token_id: str
    side: str          # "BUY" or "SELL"
    contracts: float
    price: float
    usdc_amount: float
    timestamp: float = field(default_factory=time.time)
    notes: str = ""

    @property
    def fee(self) -> float:
        # Polymarket charges ~0.2% taker fee
        return self.usdc_amount * 0.002


class PaperPortfolio:
    """
    Virtual portfolio for paper trading.
    Tracks USDC balance, positions, and full trade history.
    """

    def __init__(self, starting_balance: float = 10_000.0) -> None:
        self.usdc_balance: float = starting_balance
        self.starting_balance: float = starting_balance
        self.positions: dict[str, Position] = {}   # token_id -> Position
        self.trades: list[Trade] = []
        self.open_orders: dict[str, dict] = {}    # order_id -> order info
        self._trade_counter: int = 0
        # Time-series for charting: list of {t, value, pnl}
        self.pnl_history: list[dict] = [{"t": time.time(), "value": starting_balance, "pnl": 0.0}]

    # ------------------------------------------------------------------ #
    #  Core operations                                                      #
    # ------------------------------------------------------------------ #

    def buy(
        self,
        token_id: str,
        contracts: float,
        price: float,
        strategy: str,
        market_question: str = "",
        outcome: str = "",
        notes: str = "",
    ) -> Optional[Trade]:
        """Simulate buying outcome tokens."""
        cost = contracts * price
        fee = cost * 0.002

        if cost + fee > self.usdc_balance:
            logger.warning(
                f"[PAPER] Insufficient balance: need ${cost+fee:.2f}, have ${self.usdc_balance:.2f}"
            )
            return None

        self.usdc_balance -= (cost + fee)
        self._trade_counter += 1
        trade = Trade(
            trade_id=f"T{self._trade_counter:05d}",
            strategy=strategy,
            token_id=token_id,
            side="BUY",
            contracts=contracts,
            price=price,
            usdc_amount=cost,
            notes=notes,
        )
        self.trades.append(trade)

        # Update or create position
        if token_id in self.positions:
            pos = self.positions[token_id]
            total_contracts = pos.contracts + contracts
            pos.avg_cost = (pos.cost_basis + cost) / total_contracts
            pos.contracts = total_contracts
        else:
            self.positions[token_id] = Position(
                token_id=token_id,
                market_question=market_question,
                outcome=outcome,
                contracts=contracts,
                avg_cost=price,
                strategy=strategy,
            )

        self._update_metrics()
        logger.info(
            f"[PAPER] BUY {contracts:.2f} {outcome} @ {price:.4f} "
            f"cost=${cost:.2f} fee=${fee:.2f} balance=${self.usdc_balance:.2f} [{strategy}]"
        )
        return trade

    def sell(
        self,
        token_id: str,
        contracts: float,
        price: float,
        strategy: str,
        notes: str = "",
    ) -> Optional[Trade]:
        """Simulate selling outcome tokens."""
        pos = self.positions.get(token_id)
        if not pos or pos.contracts < contracts:
            logger.warning(
                f"[PAPER] Cannot sell {contracts:.2f} of {token_id[:16]}: "
                f"position={pos.contracts if pos else 0:.2f}"
            )
            return None

        proceeds = contracts * price
        fee = proceeds * 0.002
        net_proceeds = proceeds - fee
        realized = contracts * (price - pos.avg_cost) - fee

        self.usdc_balance += net_proceeds
        pos.contracts -= contracts
        pos.realized_pnl += realized

        if pos.contracts < 0.001:
            del self.positions[token_id]

        self._trade_counter += 1
        trade = Trade(
            trade_id=f"T{self._trade_counter:05d}",
            strategy=strategy,
            token_id=token_id,
            side="SELL",
            contracts=contracts,
            price=price,
            usdc_amount=proceeds,
            notes=notes,
        )
        self.trades.append(trade)
        self._update_metrics()

        logger.info(
            f"[PAPER] SELL {contracts:.2f} tokens @ {price:.4f} "
            f"proceeds=${net_proceeds:.2f} realized_pnl=${realized:+.2f} [{strategy}]"
        )
        return trade

    def register_limit_order(self, order_id: str, order_info: dict) -> None:
        self.open_orders[order_id] = order_info

    def cancel_limit_order(self, order_id: str) -> bool:
        return bool(self.open_orders.pop(order_id, None))

    # ------------------------------------------------------------------ #
    #  Analytics                                                            #
    # ------------------------------------------------------------------ #

    def total_value(self, price_map: dict[str, float] | None = None) -> float:
        """USDC balance + mark-to-market positions."""
        mtm = 0.0
        if price_map:
            for tid, pos in self.positions.items():
                price = price_map.get(tid, pos.avg_cost)
                mtm += pos.contracts * price
        else:
            mtm = sum(pos.cost_basis for pos in self.positions.values())
        return self.usdc_balance + mtm

    def total_pnl(self, price_map: dict[str, float] | None = None) -> float:
        return self.total_value(price_map) - self.starting_balance

    def realized_pnl(self) -> float:
        return sum(t.usdc_amount * (1 if t.side == "SELL" else -1) for t in self.trades)

    def total_fees_paid(self) -> float:
        return sum(t.fee for t in self.trades)

    def exposure(self) -> float:
        return sum(pos.cost_basis for pos in self.positions.values())

    def strategy_pnl(self) -> dict[str, float]:
        pnl: dict[str, float] = {}
        for t in self.trades:
            amt = t.usdc_amount if t.side == "SELL" else -t.usdc_amount
            pnl[t.strategy] = pnl.get(t.strategy, 0.0) + amt - t.fee
        return pnl

    def summary(self, price_map: dict[str, float] | None = None) -> str:
        total = self.total_value(price_map)
        pnl = total - self.starting_balance
        pnl_pct = (pnl / self.starting_balance) * 100
        lines = [
            "=" * 60,
            "  PAPER TRADING PORTFOLIO SUMMARY",
            "=" * 60,
            f"  Starting Balance : ${self.starting_balance:>10,.2f}",
            f"  USDC Balance     : ${self.usdc_balance:>10,.2f}",
            f"  Total Value      : ${total:>10,.2f}",
            f"  Total P&L        : ${pnl:>+10,.2f}  ({pnl_pct:+.2f}%)",
            f"  Total Fees Paid  : ${self.total_fees_paid():>10,.2f}",
            f"  Open Positions   : {len(self.positions):>10}",
            f"  Total Trades     : {len(self.trades):>10}",
            "-" * 60,
            "  Strategy P&L:",
        ]
        for strat, spnl in sorted(self.strategy_pnl().items(), key=lambda x: -x[1]):
            lines.append(f"    {strat:<30} ${spnl:>+10,.2f}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def save_to_json(self, path: str = "logs/portfolio_state.json") -> None:
        """Persist trade history and stats for the meta-agent to read."""
        import json, os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "snapshot_time": time.time(),
            "starting_balance": self.starting_balance,
            "usdc_balance": self.usdc_balance,
            "total_value": self.total_value(),
            "total_pnl": self.total_pnl(),
            "fees_paid": self.total_fees_paid(),
            "open_positions": len(self.positions),
            "strategy_pnl": self.strategy_pnl(),
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "strategy": t.strategy,
                    "side": t.side,
                    "contracts": t.contracts,
                    "price": t.price,
                    "usdc_amount": t.usdc_amount,
                    "fee": t.fee,
                    "timestamp": t.timestamp,
                    "notes": t.notes,
                }
                for t in self.trades
            ],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _update_metrics(self) -> None:
        portfolio_balance.set(self.usdc_balance)
        portfolio_pnl.set(self.total_pnl())
        open_positions.set(len(self.positions))
        total_exposure.set(self.exposure())
        # Record time-series point (max 2000 points)
        self.pnl_history.append({"t": time.time(), "value": round(self.total_value(), 2), "pnl": round(self.total_pnl(), 2)})
        if len(self.pnl_history) > 2000:
            self.pnl_history = self.pnl_history[-2000:]
