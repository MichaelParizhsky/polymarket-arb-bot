"""
Paper trading engine.
Tracks virtual positions, P&L, and order history.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from src.utils.logger import logger
from src.utils.metrics import (
    portfolio_balance, portfolio_pnl, open_positions, total_exposure
)
from src.utils.database import log_trade, init_db


def _estimate_slippage(volume_24h: float) -> float:
    """Liquidity-adjusted slippage estimate."""
    if volume_24h >= 100_000:
        return 0.002   # 0.2% — liquid market
    elif volume_24h >= 10_000:
        return 0.008   # 0.8% — medium market
    elif volume_24h >= 1_000:
        return 0.020   # 2.0% — thin market
    else:
        return 0.040   # 4.0% — very thin, likely partial fill


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
    end_date_iso: str = ""   # ISO date string for market resolution (optional)

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
        # Closed positions history for win-rate and realized P&L tracking
        self.closed_positions: list[dict] = []
        init_db()

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
        market=None,
    ) -> Optional[Trade]:
        """Simulate buying outcome tokens."""
        volume_24h = market.get("volume_24h", 0.0) if isinstance(market, dict) else getattr(market, "volume_24h", 0.0) if market else 0.0
        slippage_rate = _estimate_slippage(volume_24h)
        slippage = price * slippage_rate
        cost = contracts * (price + slippage)
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
            f"cost=${cost:.2f} fee=${fee:.2f} slippage={slippage_rate:.1%} balance=${self.usdc_balance:.2f} [{strategy}]"
        )
        try:
            log_trade(
                strategy=strategy,
                token_id=token_id,
                side="BUY",
                usdc_amount=cost,
                price=price,
                contracts=contracts,
                fee=fee,
                slippage=slippage,
                is_paper=True,
            )
        except Exception as exc:
            logger.warning(f"[PAPER] DB log_trade failed: {exc}")
        return trade

    def sell(
        self,
        token_id: str,
        contracts: float,
        price: float,
        strategy: str,
        notes: str = "",
        market=None,
    ) -> Optional[Trade]:
        """Simulate selling outcome tokens."""
        pos = self.positions.get(token_id)
        if not pos or pos.contracts < contracts:
            logger.warning(
                f"[PAPER] Cannot sell {contracts:.2f} of {token_id[:16]}: "
                f"position={pos.contracts if pos else 0:.2f}"
            )
            return None

        volume_24h = market.get("volume_24h", 0.0) if isinstance(market, dict) else getattr(market, "volume_24h", 0.0) if market else 0.0
        slippage_rate = _estimate_slippage(volume_24h)
        slippage = price * slippage_rate
        proceeds = contracts * (price - slippage)
        fee = proceeds * 0.002
        net_proceeds = proceeds - fee
        realized = contracts * (price - pos.avg_cost) - fee

        self.usdc_balance += net_proceeds
        pos.contracts -= contracts
        pos.realized_pnl += realized

        if pos.contracts < 0.001:
            self.closed_positions.append({
                "token_id": token_id,
                "market_question": pos.market_question,
                "outcome": pos.outcome,
                "strategy": pos.strategy,
                "realized_pnl": round(pos.realized_pnl, 4),
                "opened_at": pos.opened_at,
                "closed_at": time.time(),
            })
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
            f"proceeds=${net_proceeds:.2f} realized_pnl=${realized:+.2f} slippage={slippage_rate:.1%} [{strategy}]"
        )
        try:
            log_trade(
                strategy=strategy,
                token_id=token_id,
                side="SELL",
                usdc_amount=proceeds,
                price=price,
                contracts=contracts,
                fee=fee,
                slippage=slippage,
                is_paper=True,
            )
        except Exception as exc:
            logger.warning(f"[PAPER] DB log_trade failed: {exc}")
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
        """Fee-adjusted realized P&L: closed positions + partial sells on open positions."""
        closed = self.realized_closed_pnl()
        partial = sum(pos.realized_pnl for pos in self.positions.values())
        return closed + partial

    def total_fees_paid(self) -> float:
        return sum(t.fee for t in self.trades)

    def exposure(self) -> float:
        return sum(pos.cost_basis for pos in self.positions.values())

    def realized_closed_pnl(self) -> float:
        """Sum of P&L from fully-closed positions only."""
        return sum(p["realized_pnl"] for p in self.closed_positions)

    def unrealized_pnl(self) -> float:
        """Estimated unrealized P&L: open positions valued at cost basis minus their share of capital."""
        return self.total_value() - self.usdc_balance - self.exposure()

    def win_rate(self) -> float:
        """% of closed positions that were profitable."""
        if not self.closed_positions:
            return 0.0
        wins = sum(1 for p in self.closed_positions if p["realized_pnl"] > 0)
        return round(wins / len(self.closed_positions) * 100, 1)

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
        """Persist trade history and stats for the meta-agent to read.

        Only the most recent trades and closed positions are saved to keep
        the file size bounded. The meta-agent only needs recent data for
        rolling strategy analysis; older history doesn't improve decisions.
        """
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        # Rolling windows — enough for meaningful analysis, bounded disk use
        MAX_TRADES = 500
        MAX_CLOSED = 200
        MAX_PNL_HISTORY = 200

        recent_trades = self.trades[-MAX_TRADES:]
        recent_closed = self.closed_positions[-MAX_CLOSED:]
        recent_pnl = self.pnl_history[-MAX_PNL_HISTORY:]

        data = {
            "snapshot_time": time.time(),
            "starting_balance": self.starting_balance,
            "usdc_balance": self.usdc_balance,
            "total_value": self.total_value(),
            "total_pnl": self.total_pnl(),
            "total_trades_all_time": len(self.trades),
            "fees_paid": self.total_fees_paid(),
            "open_positions": len(self.positions),
            "strategy_pnl": self.strategy_pnl(),
            "realized_closed_pnl": self.realized_closed_pnl(),
            "win_rate": self.win_rate(),
            "closed_positions": recent_closed,
            "pnl_history": recent_pnl,
            # Persist positions directly so load_from_json doesn't have to
            # reconstruct them from a truncated trade history window.
            "positions": {
                tid: {
                    "token_id": pos.token_id,
                    "market_question": pos.market_question,
                    "outcome": pos.outcome,
                    "contracts": round(pos.contracts, 6),
                    "avg_cost": round(pos.avg_cost, 6),
                    "strategy": pos.strategy,
                    "opened_at": pos.opened_at,
                    "realized_pnl": round(pos.realized_pnl, 6),
                    "end_date_iso": pos.end_date_iso,
                }
                for tid, pos in self.positions.items()
            },
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "strategy": t.strategy,
                    "side": t.side,
                    "contracts": round(t.contracts, 6),
                    "price": round(t.price, 6),
                    "usdc_amount": round(t.usdc_amount, 4),
                    "fee": round(t.fee, 6),
                    "timestamp": t.timestamp,
                    "notes": t.notes,
                }
                for t in recent_trades
            ],
        }
        import tempfile
        abs_path = os.path.abspath(path)
        dir_name = os.path.dirname(abs_path)
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            tmp_path = tmp.name
            json.dump(data, tmp, separators=(",", ":"))  # compact JSON, no indent
        os.replace(tmp_path, abs_path)  # atomic on POSIX, best-effort on Windows

    def load_from_json(self, path: str = "logs/portfolio_state.json") -> bool:
        """Restore portfolio state from a previous run. Returns True if loaded."""
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            self.usdc_balance = data["usdc_balance"]
            self.starting_balance = data["starting_balance"]
            self.trades = []
            for t in data.get("trades", []):
                trade = Trade(
                    trade_id=t["trade_id"],
                    strategy=t["strategy"],
                    token_id=t.get("token_id", ""),
                    side=t["side"],
                    contracts=t["contracts"],
                    price=t["price"],
                    usdc_amount=t["usdc_amount"],
                    timestamp=t["timestamp"],
                    notes=t.get("notes", ""),
                )
                self.trades.append(trade)
            self._trade_counter = len(self.trades)
            self.closed_positions = data.get("closed_positions", [])

            # Restore positions: prefer the saved positions dict (exact state),
            # fall back to reconstructing from trade history for old save files.
            if "positions" in data:
                self.positions = {}
                for tid, p in data["positions"].items():
                    self.positions[tid] = Position(
                        token_id=p["token_id"],
                        market_question=p.get("market_question", ""),
                        outcome=p.get("outcome", ""),
                        contracts=p["contracts"],
                        avg_cost=p["avg_cost"],
                        strategy=p.get("strategy", ""),
                        opened_at=p.get("opened_at", time.time()),
                        realized_pnl=p.get("realized_pnl", 0.0),
                        end_date_iso=p.get("end_date_iso", ""),
                    )
            else:
                # Legacy fallback: reconstruct from truncated trade history
                self.positions = {}
                for t in self.trades:
                    if t.side == "BUY":
                        if t.token_id in self.positions:
                            pos = self.positions[t.token_id]
                            total = pos.contracts + t.contracts
                            pos.avg_cost = (pos.cost_basis + t.usdc_amount) / total
                            pos.contracts = total
                        else:
                            self.positions[t.token_id] = Position(
                                token_id=t.token_id,
                                market_question="",
                                outcome="",
                                contracts=t.contracts,
                                avg_cost=t.price,
                                strategy=t.strategy,
                                opened_at=t.timestamp,
                            )
                    elif t.side == "SELL" and t.token_id in self.positions:
                        pos = self.positions[t.token_id]
                        pos.contracts -= t.contracts
                        if pos.contracts < 0.001:
                            del self.positions[t.token_id]
            logger.info(
                f"[PAPER] Restored state: {len(self.trades)} trades, "
                f"{len(self.positions)} positions, balance=${self.usdc_balance:.2f}"
            )
            return True
        except Exception as exc:
            logger.warning(f"[PAPER] Could not load state: {exc}")
            return False

    def _update_metrics(self) -> None:
        portfolio_balance.set(self.usdc_balance)
        portfolio_pnl.set(self.total_pnl())
        open_positions.set(len(self.positions))
        total_exposure.set(self.exposure())
        # Record time-series point (max 2000 points)
        self.pnl_history.append({"t": time.time(), "value": round(self.total_value(), 2), "pnl": round(self.total_pnl(), 2)})
        if len(self.pnl_history) > 500:
            self.pnl_history = self.pnl_history[-500:]
