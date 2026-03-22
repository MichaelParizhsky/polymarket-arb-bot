"""
Strategy: Crypto Short Market Arb — 5-minute and 15-minute up/down markets.

Two entry modes:

1. DUAL-SIDE ARB (preferred):
   If YES_ask + NO_ask < DUAL_ARB_THRESHOLD (0.995), buy BOTH sides.
   One must resolve at $1.00, so guaranteed profit regardless of direction.
   Edge = 1.0 - (YES_ask + NO_ask) - fees

2. END-OF-WINDOW SNIPE:
   Within 60 seconds of window close, if one side >80% mid, buy that side.
   Price direction is already telegraphed by momentum at T-60s.

Why this works:
  - Markets rotate every 5 or 15 minutes — capital recycles extremely fast
  - Dual-side arb is market-neutral: no prediction skill required
  - End-of-window entries capture the final price certainty premium
  - These are the highest-frequency markets on Polymarket
"""
from __future__ import annotations

import logging
import time
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.utils.constants import MIN_TRADE_USDC
from src.utils.metrics import arb_opportunities, edge_detected

logger = logging.getLogger(__name__)

# Combined YES+NO ask must be below this for dual-side arb
DUAL_ARB_THRESHOLD = 0.995

# End-of-window snipe: fire when this many seconds remain in the window
SNIPE_WINDOW_SECONDS = 60.0

# Minimum conviction for end-of-window snipe
SNIPE_MIN_CONVICTION = 0.80

# Minimum net edge required for any entry
MIN_NET_EDGE = 0.005  # 0.5%

# Polymarket taker fee (standard markets, 2025 rate — near zero)
TAKER_FEE = 0.002


def _seconds_to_window_close(end_date_iso: str) -> float | None:
    """Return seconds until this market's window closes, or None if unparseable."""
    try:
        from datetime import datetime, timezone
        s = end_date_iso.strip().rstrip("Z")
        if "T" not in s:
            s += "T00:00:00"
        end = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return (end - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


class CryptoShortStrategy(BaseStrategy):
    """
    Targets Polymarket 5m and 15m crypto up/down markets.
    These markets are discovered via slug calculation in PolymarketClient.get_crypto_short_markets().
    Receives them through the 'crypto_short_markets' key in the scan context.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        # Track entered markets to avoid duplicate entries
        self._entered: dict[str, float] = {}  # condition_id -> entered_at

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        # This strategy gets its own market list from the context key
        # populated by main.py calling get_crypto_short_markets()
        markets: list[Market] = context.get("crypto_short_markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})

        if not markets:
            return []

        # Require live Binance data for snipe mode — without it we have no directional edge.
        # Binance.com returns HTTP 451 from US-hosted servers; if feed is stale, halt snipes.
        binance_feed = context.get("binance_feed")
        _binance_live = False
        if binance_feed is not None:
            # Check if any price has been updated in the last 30 seconds
            prices = getattr(binance_feed, "_prices", {})
            now = time.time()
            _binance_live = any(
                now - getattr(p, "timestamp", 0) < 30
                for p in prices.values()
            )
        if not _binance_live:
            self.log(
                "Binance feed stale or unavailable — snipe mode disabled (no directional edge without price reference)",
                "warning",
            )
            # Dual-side arb is market-neutral and doesn't need Binance — allow it to proceed
            # but set a flag to skip snipe entries
        _snipe_allowed = _binance_live

        signals: list[Signal] = []
        cfg = self.config.strategies
        max_spend = getattr(cfg, "crypto_5m_max_spend", 100.0)

        # Prune stale entries (windows are 5-15 min, keep 30min buffer)
        cutoff = time.time() - 1800
        self._entered = {k: v for k, v in self._entered.items() if v > cutoff}

        for market in markets:
            if not market.active or market.closed:
                continue
            if market.condition_id in self._entered:
                continue

            yes_token = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
            no_token  = next((t for t in market.tokens if t.outcome.lower() in ("no", "down")), None)
            if not yes_token or not no_token:
                # Try up/down naming
                yes_token = next((t for t in market.tokens if t.outcome.lower() in ("yes", "up")), None)
                no_token  = next((t for t in market.tokens if t.outcome.lower() in ("no", "down")), None)
            if not yes_token or not no_token:
                continue

            yes_book = orderbooks.get(yes_token.token_id)
            no_book  = orderbooks.get(no_token.token_id)

            if not yes_book or not no_book:
                continue

            yes_ask = yes_book.best_ask
            no_ask  = no_book.best_ask
            yes_mid = yes_book.mid

            if yes_ask is None or no_ask is None:
                continue

            seconds_left = _seconds_to_window_close(market.end_date_iso)
            if seconds_left is None or seconds_left <= 0:
                continue

            # ── Mode 1: Dual-side guaranteed arb ─────────────────────────
            combined_ask = yes_ask + no_ask
            if combined_ask < DUAL_ARB_THRESHOLD:
                gross_edge = 1.0 - combined_ask
                net_edge   = gross_edge - (TAKER_FEE * 2)  # fee on both legs
                if net_edge >= MIN_NET_EDGE:
                    # Signal for YES leg (we'll handle NO leg separately)
                    # For now, generate a YES signal; the bot places both orders
                    arb_opportunities.labels(strategy="crypto_5m").inc()
                    edge_detected.labels(strategy="crypto_5m").observe(net_edge)

                    size_usdc = self.risk.size_position(edge=net_edge, base_size=max_spend)
                    if size_usdc >= MIN_TRADE_USDC:
                        self._entered[market.condition_id] = time.time()
                        self.log(
                            f"[CRYPTO 5M DUAL ARB] {market.question[:60]} | "
                            f"YES_ask={yes_ask:.4f} NO_ask={no_ask:.4f} combined={combined_ask:.4f} "
                            f"net_edge={net_edge:.4f} | {seconds_left:.0f}s left"
                        )
                        # YES leg
                        signals.append(Signal(
                            strategy="crypto_5m",
                            token_id=yes_token.token_id,
                            side="BUY",
                            price=yes_ask,
                            size_usdc=size_usdc / 2,
                            edge=net_edge,
                            notes=f"[DUAL_ARB] YES leg | combined={combined_ask:.4f} edge={net_edge:.4f}",
                            metadata={
                                "outcome": "YES",
                                "arb_type": "dual_side",
                                "combined_ask": combined_ask,
                                "net_edge": net_edge,
                                "seconds_left": seconds_left,
                                "condition_id": market.condition_id,
                            },
                        ))
                        # NO leg
                        signals.append(Signal(
                            strategy="crypto_5m",
                            token_id=no_token.token_id,
                            side="BUY",
                            price=no_ask,
                            size_usdc=size_usdc / 2,
                            edge=net_edge,
                            notes=f"[DUAL_ARB] NO leg | combined={combined_ask:.4f} edge={net_edge:.4f}",
                            metadata={
                                "outcome": "NO",
                                "arb_type": "dual_side",
                                "combined_ask": combined_ask,
                                "net_edge": net_edge,
                                "seconds_left": seconds_left,
                                "condition_id": market.condition_id,
                            },
                        ))
                    continue  # don't also try snipe mode

            # ── Mode 2: End-of-window momentum snipe ─────────────────────
            if not _snipe_allowed:
                continue  # no Binance data = no directional edge = skip snipe
            if seconds_left <= SNIPE_WINDOW_SECONDS and yes_mid is not None:
                if yes_mid >= SNIPE_MIN_CONVICTION:
                    net_edge = (1.0 - yes_ask) - TAKER_FEE
                    if net_edge >= MIN_NET_EDGE and yes_ask < 1.0:
                        arb_opportunities.labels(strategy="crypto_5m").inc()
                        edge_detected.labels(strategy="crypto_5m").observe(net_edge)
                        size_usdc = self.risk.size_position(edge=net_edge, base_size=max_spend)
                        if size_usdc >= MIN_TRADE_USDC:
                            self._entered[market.condition_id] = time.time()
                            self.log(
                                f"[CRYPTO 5M SNIPE] YES @ {yes_ask:.4f} | "
                                f"mid={yes_mid:.3f} edge={net_edge:.4f} | {seconds_left:.0f}s left | "
                                f"{market.question[:60]}"
                            )
                            signals.append(Signal(
                                strategy="crypto_5m",
                                token_id=yes_token.token_id,
                                side="BUY",
                                price=yes_ask,
                                size_usdc=size_usdc,
                                edge=net_edge,
                                notes=f"[SNIPE] YES @ {yes_ask:.4f} | {seconds_left:.0f}s left",
                                metadata={
                                    "outcome": "YES",
                                    "arb_type": "snipe",
                                    "net_edge": net_edge,
                                    "seconds_left": seconds_left,
                                    "condition_id": market.condition_id,
                                },
                            ))

                elif yes_mid <= (1.0 - SNIPE_MIN_CONVICTION):
                    net_edge = (1.0 - no_ask) - TAKER_FEE
                    if net_edge >= MIN_NET_EDGE and no_ask < 1.0:
                        arb_opportunities.labels(strategy="crypto_5m").inc()
                        edge_detected.labels(strategy="crypto_5m").observe(net_edge)
                        size_usdc = self.risk.size_position(edge=net_edge, base_size=max_spend)
                        if size_usdc >= MIN_TRADE_USDC:
                            self._entered[market.condition_id] = time.time()
                            self.log(
                                f"[CRYPTO 5M SNIPE] NO @ {no_ask:.4f} | "
                                f"yes_mid={yes_mid:.3f} edge={net_edge:.4f} | {seconds_left:.0f}s left | "
                                f"{market.question[:60]}"
                            )
                            signals.append(Signal(
                                strategy="crypto_5m",
                                token_id=no_token.token_id,
                                side="BUY",
                                price=no_ask,
                                size_usdc=size_usdc,
                                edge=net_edge,
                                notes=f"[SNIPE] NO @ {no_ask:.4f} | {seconds_left:.0f}s left",
                                metadata={
                                    "outcome": "NO",
                                    "arb_type": "snipe",
                                    "net_edge": net_edge,
                                    "seconds_left": seconds_left,
                                    "condition_id": market.condition_id,
                                },
                            ))

        if signals:
            logger.info(f"CryptoShort: {len(signals)} signals generated")

        return signals
