"""
Strategy 4: Market Making.

Posts two-sided limit orders (bid + ask) on high-volume markets to earn
the spread. Manages inventory risk by skewing quotes when imbalanced.

Key mechanics:
  - Select markets with high volume/liquidity
  - Post bid slightly below mid, ask slightly above mid
  - Adjust quote sizes based on inventory (skew to rebalance)
  - Cancel and refresh quotes every N seconds
  - Hard inventory limit to cap directional risk
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.utils.metrics import mm_quotes_placed, mm_inventory


# Minimum volume (USDC) for market making eligibility
# Configurable via env var MM_MIN_VOLUME (default 50k for live, lower in paper mode)
import os as _os
MIN_VOLUME_FOR_MM = float(_os.getenv("MM_MIN_VOLUME", "50000"))


class MarketMakingStrategy(BaseStrategy):
    """
    Passive market maker for Polymarket binary markets.
    Earns spread on both YES and NO tokens.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        # Track our active MM quotes: token_id -> {bid_order, ask_order, last_refresh}
        self._quotes: dict[str, dict] = {}
        self._last_refresh: dict[str, float] = {}
        self._refresh_interval: float = 10.0   # seconds between quote refresh
        self._price_cache: dict[str, list[float]] = {}  # token_id -> recent mid prices

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        signals: list[Signal] = []

        # Select eligible markets
        eligible = self._select_markets(markets, orderbooks)
        eligible_ids = {
            next(
                (t.token_id for t in m.tokens if t.outcome.lower() == "yes"),
                m.tokens[0].token_id if m.tokens else None,
            )
            for m in eligible
        } - {None}

        # Prune stale entries for markets no longer being made
        stale = [tid for tid in self._last_refresh if tid not in eligible_ids]
        for tid in stale:
            self._last_refresh.pop(tid, None)
            self._quotes.pop(tid, None)

        for market in eligible:
            yes_tok = next(
                (t for t in market.tokens if t.outcome.lower() == "yes"),
                market.tokens[0] if market.tokens else None,
            )
            if not yes_tok:
                continue

            token_id = yes_tok.token_id
            book = orderbooks.get(token_id)
            if not book or book.mid is None:
                continue

            now = time.time()
            last = self._last_refresh.get(token_id, 0)
            if now - last < self._refresh_interval:
                continue

            # Record current mid in price history (cap at 60 entries ~10 min at 10s refresh)
            if book.mid is not None:
                history = self._price_cache.setdefault(token_id, [])
                history.append(book.mid)
                if len(history) > 60:
                    del history[:-60]

            # Adverse selection check — skip market if risk is too high
            price_history = self._price_cache.get(token_id, [])
            as_score = self._compute_adverse_selection_score(market.condition_id, book, price_history)
            AS_THRESHOLD = getattr(self.config.strategy, 'mm_adverse_selection_threshold', 0.6) if hasattr(self.config, 'strategy') else 0.6
            if as_score >= AS_THRESHOLD:
                self.log(
                    f"MM: skipping {market.question[:40]} — adverse selection score {as_score:.2f} >= {AS_THRESHOLD:.2f}",
                    "debug",
                )
                continue

            # Inventory skew check — stop posting bids if too long
            yes_token_id = token_id
            inventory_skew = self._compute_inventory_skew(yes_token_id, context.get('portfolio'))
            if inventory_skew > 0.7:
                self.log(
                    f"MM: skipping bid for {market.question[:40]} — inventory skew {inventory_skew:.2f} too long",
                    "debug",
                )
                # Only post asks (selling side) when inventory is heavy
                # Skip bid side

            # Check existing quotes — cancel if they've moved too far from mid
            if token_id in self._quotes:
                signals.extend(self._cancel_stale_quotes(token_id, book))

            # Generate new quotes
            new_signals = self._generate_quotes(market, token_id, book)
            if new_signals:
                signals.extend(new_signals)
                self._last_refresh[token_id] = now

        return signals

    def _compute_adverse_selection_score(self, market_id: str, orderbook, price_history: list[float]) -> float:
        """
        Estimate adverse selection risk for a market (0.0 = low risk, 1.0 = high risk).

        Adverse selection occurs when informed traders take your limit orders because
        they know the market will move against you.

        Signals of high adverse selection:
        - Recent price velocity (large moves = informed trading)
        - Order book imbalance (one side much thicker = directional pressure)
        - Thin spread (other MMs already competed away the edge)
        """
        score = 0.0

        # 1. Price velocity check
        if len(price_history) >= 2:
            recent_move = abs(price_history[-1] - price_history[-min(len(price_history), 12)])
            if recent_move > 0.05:   # >5% move recently
                score += 0.5
            elif recent_move > 0.02:  # >2% move recently
                score += 0.25

        # 2. Order book imbalance check
        if orderbook and hasattr(orderbook, 'bids') and hasattr(orderbook, 'asks'):
            bid_depth = sum(getattr(level, 'size', 0) for level in (orderbook.bids or [])[:5])
            ask_depth = sum(getattr(level, 'size', 0) for level in (orderbook.asks or [])[:5])
            total_depth = bid_depth + ask_depth
            if total_depth > 0:
                imbalance = abs(bid_depth - ask_depth) / total_depth
                if imbalance > 0.7:   # 70%+ imbalance = strong directional pressure
                    score += 0.4
                elif imbalance > 0.5:
                    score += 0.2

        # 3. Spread tightness (if spread is very tight, other MMs are already there)
        if orderbook:
            best_bid = getattr(orderbook, 'best_bid', 0) or 0
            best_ask = getattr(orderbook, 'best_ask', 1) or 1
            spread = best_ask - best_bid
            if spread < 0.02:   # <2% spread — already crowded
                score += 0.2

        return min(score, 1.0)

    def _compute_inventory_skew(self, token_id: str, portfolio) -> float:
        """
        Compute how skewed our inventory is for this token.
        Returns: positive = long heavy, negative = short heavy
        Range: -1.0 to 1.0

        If we're long heavy (holding lots of YES), we should skew ask price lower
        and bid price higher to reduce inventory. Stop posting new bids at 0.7+ skew.
        """
        if portfolio is None:
            return 0.0

        positions = getattr(portfolio, 'positions', None) or {}
        position = positions.get(token_id)
        if not position:
            return 0.0

        if isinstance(position, dict):
            contracts = position.get('contracts', 0) or 0
        else:
            contracts = getattr(position, 'contracts', 0) or 0

        max_inventory = 500.0  # $500 USD equivalent

        skew = min(contracts / max_inventory, 1.0) if contracts > 0 else 0.0
        return skew

    def _select_markets(
        self, markets: list[Market], orderbooks: dict[str, Orderbook]
    ) -> list[Market]:
        """Pick top markets by volume that have reasonable spreads."""
        cfg = self.config.strategies
        max_spread = getattr(cfg, "mm_max_market_spread_pct", 0.06)
        eligible = []
        for m in markets:
            if not m.active or m.closed:
                continue
            # Skip markets whose end date has already passed — Polymarket sometimes
            # keeps them "active" while awaiting official resolution.
            if m.end_date_iso:
                try:
                    end_dt = datetime.fromisoformat(m.end_date_iso.rstrip("Z")).replace(tzinfo=timezone.utc)
                    if end_dt < datetime.now(timezone.utc):
                        continue
                except ValueError:
                    pass
            if m.volume < MIN_VOLUME_FOR_MM:
                continue
            yes_tok = next(
                (t for t in m.tokens if t.outcome.lower() == "yes"),
                m.tokens[0] if m.tokens else None,
            )
            if not yes_tok:
                continue
            book = orderbooks.get(yes_tok.token_id)
            if not book or book.mid is None:
                continue
            # Only make markets where mid is not at extremes (avoid near-resolved markets)
            if not (0.05 < book.mid < 0.95):
                continue
            # Skip markets with a wide bid-ask spread — adverse selection risk
            if book.best_bid is not None and book.best_ask is not None:
                market_spread = book.best_ask - book.best_bid
                if market_spread > max_spread:
                    self.log(
                        f"MM skip {m.question[:40]} — spread {market_spread:.3f} > {max_spread:.3f}",
                        "debug",
                    )
                    continue
            eligible.append(m)

        # Sort by volume descending, take top 10
        eligible.sort(key=lambda m: m.volume, reverse=True)
        return eligible[:10]

    def _generate_quotes(
        self, market: Market, token_id: str, book: Orderbook
    ) -> list[Signal]:
        """Generate bid + ask limit orders with inventory-skewed prices."""
        cfg = self.config.strategies
        signals = []

        mid = book.mid
        spread_bps = max(cfg.mm_spread_bps, getattr(cfg, "mm_min_spread_bps", 10))
        half_spread = (spread_bps / 10000) / 2

        # Inventory skew: if we hold a lot of YES, widen bid, tighten ask
        pos = self.portfolio.positions.get(token_id)
        inventory = pos.contracts if pos else 0.0
        max_inv = cfg.mm_max_inventory
        inv_ratio = min(max(inventory / max_inv, -1.0), 1.0) if max_inv > 0 else 0.0

        # Hard inventory skew limit — stop quoting the adverse side beyond threshold
        skew_limit = getattr(cfg, "mm_inventory_skew_limit", 0.30)

        # Skew the mid price to reflect inventory
        skew_factor = cfg.mm_skew_factor
        skewed_mid = mid - inv_ratio * skew_factor * half_spread

        bid_price = round(max(skewed_mid - half_spread, 0.01), 4)
        ask_price = round(min(skewed_mid + half_spread, 0.99), 4)

        if ask_price <= bid_price:
            return signals

        order_size = cfg.mm_order_size
        available = self.portfolio.usdc_balance

        # Check risk limits
        ok, reason = self.risk.check_trade(token_id, "BUY", order_size, "market_making")
        if not ok:
            self.log(f"MM bid blocked: {reason}", "debug")
        elif inv_ratio > skew_limit:
            # Already long too much YES — skip bid to reduce inventory risk
            self.log(
                f"MM bid skipped — inventory skew {inv_ratio:.2f} > {skew_limit:.2f}",
                "debug",
            )
        else:
            bid_usdc = min(order_size, available * 0.1)
            bid_contracts = bid_usdc / bid_price

            self.log(
                f"MM BID {market.question[:40]} | "
                f"bid={bid_price:.4f} ask={ask_price:.4f} mid={mid:.4f} "
                f"inv={inventory:.1f} skew={inv_ratio:+.2f}"
            )
            signals.append(Signal(
                strategy="market_making",
                token_id=token_id,
                side="BUY",
                price=bid_price,
                size_usdc=bid_usdc,
                edge=half_spread,
                notes=f"MM bid | spread={spread_bps}bps | inv={inventory:.1f}",
                metadata={
                    "order_type": "limit",
                    "paired_ask_price": ask_price,
                    "mid": mid,
                    "inventory": inventory,
                },
            ))
            mm_quotes_placed.labels(side="bid").inc()

        # Ask side — only post if inventory > 0 (we have tokens to sell)
        # In paper mode we simulate fill, so post both sides
        ok_ask, reason_ask = self.risk.check_trade(token_id, "SELL", order_size, "market_making")
        if not ok_ask:
            self.log(f"MM ask blocked: {reason_ask}", "debug")
        else:
            ask_contracts = order_size / ask_price
            if inventory >= ask_contracts or self.config.paper_trading:
                signals.append(Signal(
                    strategy="market_making",
                    token_id=token_id,
                    side="SELL",
                    price=ask_price,
                    size_usdc=order_size,
                    edge=half_spread,
                    notes=f"MM ask | spread={spread_bps}bps | inv={inventory:.1f}",
                    metadata={
                        "order_type": "limit",
                        "paired_bid_price": bid_price,
                        "mid": mid,
                        "inventory": inventory,
                    },
                ))
                mm_quotes_placed.labels(side="ask").inc()

        # Store quote info
        self._quotes[token_id] = {
            "bid": bid_price,
            "ask": ask_price,
            "mid_at_quote": mid,
            "time": time.time(),
        }
        mm_inventory.set(sum(
            self.portfolio.positions[tid].cost_basis
            for tid in self._quotes
            if tid in self.portfolio.positions
        ))

        return signals

    def _cancel_stale_quotes(
        self, token_id: str, book: Orderbook
    ) -> list[Signal]:
        """
        If mid has moved more than 1 spread away from our quote mid,
        signal cancellation (in paper mode this is a no-op).
        """
        quote = self._quotes.get(token_id)
        if not quote or not book.mid:
            return []

        drift = abs(book.mid - quote["mid_at_quote"])
        half_spread = (self.config.strategies.mm_spread_bps / 10000) / 2

        if drift > half_spread * 2:
            self.log(
                f"Stale quote for {token_id[:16]}: mid drifted {drift:.4f}, refreshing",
                "debug",
            )
            del self._quotes[token_id]

        return []   # Cancel signals would call exchange.cancel_order in live mode
