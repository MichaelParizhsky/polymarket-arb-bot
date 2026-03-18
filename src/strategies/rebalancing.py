"""
Strategy 1: Market Rebalancing Arbitrage (Sum < $1).

A binary market's YES + NO tokens must sum to $1 at resolution.
If YES_ask + NO_ask < 1.0, buying both locks in a risk-free profit.
If YES_bid + NO_bid > 1.0, selling both also locks in a profit.

Edge = |sum - 1.0| - fees - slippage
"""
from __future__ import annotations

from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.utils.constants import FEE_RATE, SLIPPAGE_RATE
from src.utils.metrics import arb_opportunities, arb_executed, edge_detected


class RebalancingStrategy(BaseStrategy):
    """
    Buy YES + NO when sum of asks < 1.0 (both sides available at a discount).
    Short YES + NO (via SELL) when sum of bids > 1.0 (both overpriced).
    """

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        signals: list[Signal] = []

        # _check_market does no I/O — run synchronously to avoid task scheduling overhead
        for m in markets:
            if m.active and not m.closed:
                try:
                    signals.extend(self._check_market(m, context))
                except Exception as exc:
                    self.log(f"Market check error: {exc}", "debug")

        return signals

    def _check_market(self, market: Market, context: dict) -> list[Signal]:
        """Check a single market for YES+NO imbalance."""
        if len(market.tokens) < 2:
            return []

        # Find YES and NO tokens
        yes_token = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
        no_token = next((t for t in market.tokens if t.outcome.lower() == "no"), None)
        if not yes_token or not no_token:
            return []

        # Get orderbooks
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        yes_book = orderbooks.get(yes_token.token_id)
        no_book = orderbooks.get(no_token.token_id)

        if not yes_book or not no_book:
            return []

        signals = []

        # --- Case 1: Buy YES + NO (sum of asks < 1) ---
        if yes_book.best_ask and no_book.best_ask:
            ask_sum = yes_book.best_ask + no_book.best_ask
            gross_edge = 1.0 - ask_sum
            net_edge = gross_edge - 2 * (FEE_RATE + SLIPPAGE_RATE)

            min_edge = self.config.strategies.rebalancing_min_edge
            if net_edge >= min_edge:
                arb_opportunities.labels(strategy="rebalancing").inc()
                edge_detected.labels(strategy="rebalancing").observe(net_edge)

                max_spend = self.config.strategies.rebalancing_max_spend
                # Limit by available size on each side
                yes_size = min(yes_book.asks[0].size if yes_book.asks else 0, max_spend / yes_book.best_ask)
                no_size = min(no_book.asks[0].size if no_book.asks else 0, max_spend / no_book.best_ask)
                contracts = min(yes_size, no_size)
                if contracts < 1.0:
                    return []

                size_usdc = self.risk.size_position(
                    edge=net_edge,
                    base_size=min(contracts * yes_book.best_ask, max_spend)
                )
                if size_usdc < 1.0:
                    return []

                # Both legs must buy the same number of contracts so they cancel at resolution.
                actual_contracts = size_usdc / yes_book.best_ask
                yes_leg_usdc = actual_contracts * yes_book.best_ask
                no_leg_usdc = actual_contracts * no_book.best_ask

                self.log(
                    f"LONG REBAL | {market.question[:60]} | "
                    f"YES@{yes_book.best_ask:.3f} + NO@{no_book.best_ask:.3f} = {ask_sum:.3f} | "
                    f"edge={net_edge:.3f} | contracts={actual_contracts:.2f}"
                )
                # Two signals: buy YES and buy NO (same contract count on each leg)
                signals.append(Signal(
                    strategy="rebalancing",
                    token_id=yes_token.token_id,
                    side="BUY",
                    price=yes_book.best_ask,
                    size_usdc=yes_leg_usdc,
                    edge=net_edge,
                    notes=f"Long rebal YES | market={market.condition_id[:8]}",
                    metadata={"pair_token_id": no_token.token_id, "ask_sum": ask_sum,
                              "contracts": actual_contracts},
                ))
                signals.append(Signal(
                    strategy="rebalancing",
                    token_id=no_token.token_id,
                    side="BUY",
                    price=no_book.best_ask,
                    size_usdc=no_leg_usdc,
                    edge=net_edge,
                    notes=f"Long rebal NO | market={market.condition_id[:8]}",
                    metadata={"pair_token_id": yes_token.token_id, "ask_sum": ask_sum,
                              "contracts": actual_contracts},
                ))

        # --- Case 2: Sell YES + Sell NO (sum of bids > 1) ---
        # Only if we hold the tokens (or in live mode where shorting is possible)
        if yes_book.best_bid and no_book.best_bid:
            bid_sum = yes_book.best_bid + no_book.best_bid
            gross_edge = bid_sum - 1.0
            net_edge = gross_edge - 2 * (FEE_RATE + SLIPPAGE_RATE)

            if net_edge >= self.config.strategies.rebalancing_min_edge:
                yes_pos = self.portfolio.positions.get(yes_token.token_id)
                no_pos = self.portfolio.positions.get(no_token.token_id)

                if yes_pos and no_pos:
                    arb_opportunities.labels(strategy="rebalancing").inc()
                    contracts = min(yes_pos.contracts, no_pos.contracts)
                    size_usdc = contracts * yes_book.best_bid

                    self.log(
                        f"SHORT REBAL | {market.question[:60]} | "
                        f"YES@{yes_book.best_bid:.3f} + NO@{no_book.best_bid:.3f} = {bid_sum:.3f} | "
                        f"edge={net_edge:.3f}"
                    )
                    signals.append(Signal(
                        strategy="rebalancing",
                        token_id=yes_token.token_id,
                        side="SELL",
                        price=yes_book.best_bid,
                        size_usdc=size_usdc,
                        edge=net_edge,
                        notes="Close rebal YES",
                    ))
                    signals.append(Signal(
                        strategy="rebalancing",
                        token_id=no_token.token_id,
                        side="SELL",
                        price=no_book.best_bid,
                        size_usdc=contracts * no_book.best_bid,
                        edge=net_edge,
                        notes="Close rebal NO",
                    ))

        return signals
