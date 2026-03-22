"""
Strategy: Quick Resolution — Fast Capital Recycling.

Targets markets resolving within a configurable window (default 6 hours),
including Polymarket's 5-minute and 15-minute crypto markets, sports markets,
and any binary market with a high-conviction price at the extremes.

Why this works:
  - Capital recycles rapidly: a 5-min market position is freed within 5 minutes
    vs. a 30-day market that ties up capital for weeks.
  - At price extremes (>0.88 or <0.12), dynamic taker fees drop to ~0.06–0.30%,
    making the net edge attractive even on small gross spreads.
  - High-conviction prices (e.g., 0.93) reflect near-certainty — the remaining
    7 cents of payout is often a free 2–5% return over minutes.

Entry logic:
  - YES side: price in [MIN_CONVICTION, 0.999] AND best_ask < (1 - MIN_EDGE)
  - NO side:  price in [0.001, 1 - MIN_CONVICTION] AND best_ask < (1 - MIN_EDGE)
  - Net edge = (1.00 - ask) - dynamic_fee(ask, market_type)

Market type detection:
  - Questions containing "5-minute", "15-minute", "5m", "15m" → crypto_5m fees
  - Questions containing sports keywords → sports fees
  - All others → standard 0.2% fee
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.strategies.latency_arb import _days_to_expiry
from src.utils.constants import calc_taker_fee, MIN_TRADE_USDC
from src.utils.metrics import arb_opportunities, edge_detected

logger = logging.getLogger(__name__)

# Regex to detect short-duration crypto market questions
_CRYPTO_SHORT_RE = re.compile(
    r"\b(5[\s-]?min|15[\s-]?min|5m|15m|hourly|1[\s-]?hour)\b",
    re.IGNORECASE,
)

_SPORTS_KEYWORDS = frozenset([
    "nba", "nfl", "nhl", "mlb", "nascar", "mls", "ufc", "boxing",
    "super bowl", "world series", "stanley cup", "playoffs", "championship",
    "win", "wins", "score", "points", "quarter", "inning", "period",
    "soccer", "football", "basketball", "baseball", "hockey", "tennis",
    "fifa", "ncaa", "march madness", "bowl game",
    "mma", "match", "game", "series",
])

_SPORTS_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _SPORTS_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _tiered_conviction(
    hours_left: float,
    base_conviction: float,
    floor: float = 0.68,
    min_edge_floor: float = 0.001,
) -> tuple[float, float]:
    """
    Returns (conviction_threshold, min_edge) tiered by time remaining.
    Closer to expiry → lower conviction needed (outcome more certain)
    AND lower required edge (less time for adverse moves).

    floor: minimum conviction allowed (lower in paper mode via QUICK_RESOLUTION_CONVICTION_FLOOR)
    min_edge_floor: minimum edge required (lower in paper mode via QUICK_RESOLUTION_MIN_EDGE)
    """
    if hours_left <= 0.5:          # <30 min
        return (max(base_conviction, floor), min_edge_floor)
    elif hours_left <= 2.0:        # <2 hours
        return (max(base_conviction - 0.06, floor), min_edge_floor)
    elif hours_left <= 6.0:        # <6 hours
        return (max(base_conviction - 0.10, floor), min_edge_floor)
    elif hours_left <= 12.0:       # <12 hours
        return (max(base_conviction - 0.14, floor), min_edge_floor)
    else:                          # 12-24 hours
        return (max(base_conviction - 0.18, floor), min_edge_floor)


def _detect_market_type(question: str) -> str:
    """Classify the fee tier based on market question text."""
    if _CRYPTO_SHORT_RE.search(question):
        return "crypto_5m"
    if _SPORTS_RE.search(question):
        return "sports"
    return "standard"


class QuickResolutionStrategy(BaseStrategy):
    """
    High-frequency near-expiry strategy targeting markets resolving within hours.

    Buys high-conviction YES or NO tokens when the ask price leaves enough net
    edge after dynamic fees. Position is naturally exited at market resolution
    ($1.00 payout) or via the auto-close loop in main.py.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        # condition_id -> (entry_price, entered_at) — one entry per market
        self._entered: dict[str, tuple[float, float]] = {}

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        signals: list[Signal] = []

        cfg = self.config.strategies
        max_hours = getattr(cfg, 'quick_resolution_max_hours', 24.0)
        min_conviction = cfg.quick_resolution_min_conviction
        conviction_floor = getattr(cfg, 'quick_resolution_conviction_floor', 0.68)
        min_edge = cfg.quick_resolution_min_edge
        max_spend = cfg.quick_resolution_max_spend

        QR_MIN_VOLUME = 10.0  # Quick resolution uses lower volume floor — sports markets are lower volume

        skipped_no_time = 0
        skipped_no_conviction = 0
        skipped_no_edge = 0
        skipped_no_volume = 0

        # Prune stale entries (markets long resolved — keep 12h window)
        cutoff = time.time() - 12 * 3600
        expired = [
            cid for cid, (_, entered_at) in self._entered.items()
            if entered_at < cutoff
        ]
        for cid in expired:
            del self._entered[cid]

        for market in markets:
            if not market.active or market.closed:
                continue

            # Only consider markets within the max_hours window
            dte_days = _days_to_expiry(market.end_date_iso)
            hours_left = dte_days * 24.0
            if hours_left > max_hours or hours_left <= 0:
                skipped_no_time += 1
                continue

            # Volume filter (lower floor for quick resolution)
            volume = market.volume or 0.0
            if volume < QR_MIN_VOLUME:
                logger.debug(
                    f"QuickRes: skip {market.question[:50]} — volume ${volume:.0f} < ${QR_MIN_VOLUME:.0f}"
                )
                skipped_no_volume += 1
                continue

            # Skip if already entered this market
            if market.condition_id in self._entered:
                continue

            yes_token = next(
                (t for t in market.tokens if t.outcome.lower() == "yes"),
                market.tokens[0] if market.tokens else None,
            )
            no_token = next(
                (t for t in market.tokens if t.outcome.lower() == "no"),
                market.tokens[1] if len(market.tokens) > 1 else None,
            )
            if not yes_token or not no_token:
                continue

            yes_book = orderbooks.get(yes_token.token_id)
            if not yes_book or yes_book.mid is None:
                continue

            yes_mid = yes_book.mid
            market_type = _detect_market_type(market.question)

            conviction_threshold, effective_min_edge = _tiered_conviction(
                hours_left, min_conviction, floor=conviction_floor, min_edge_floor=min_edge
            )

            sig = self._evaluate(
                market=market,
                yes_token_id=yes_token.token_id,
                no_token_id=no_token.token_id,
                yes_book=yes_book,
                no_book=orderbooks.get(no_token.token_id),
                yes_mid=yes_mid,
                hours_left=hours_left,
                market_type=market_type,
                min_conviction=conviction_threshold,
                min_edge=effective_min_edge,
                max_spend=max_spend,
            )
            if sig is not None:
                signals.append(sig)
            else:
                # Attribute the skip reason based on conviction vs edge
                yes_mid_val = yes_mid
                if yes_mid_val >= conviction_threshold or yes_mid_val <= (1.0 - conviction_threshold):
                    skipped_no_edge += 1
                else:
                    skipped_no_conviction += 1

        if skipped_no_time + skipped_no_conviction + skipped_no_edge + skipped_no_volume > 0:
            logger.info(
                f"QuickRes scan: {len(signals)} signals | skipped: "
                f"{skipped_no_time} time, {skipped_no_conviction} conviction, "
                f"{skipped_no_edge} edge, {skipped_no_volume} volume"
            )

        return signals

    def _evaluate(
        self,
        market: Market,
        yes_token_id: str,
        no_token_id: str,
        yes_book: Orderbook,
        no_book: Orderbook | None,
        yes_mid: float,
        hours_left: float,
        market_type: str,
        min_conviction: float,
        min_edge: float,
        max_spend: float,
    ) -> Signal | None:
        # --- YES side ---
        if yes_mid >= min_conviction:
            best_ask = yes_book.best_ask
            if best_ask is not None and best_ask < 1.0:
                fee = calc_taker_fee(best_ask, market_type)
                net_edge = (1.0 - best_ask) - fee
                if net_edge >= min_edge:
                    return self._make_signal(
                        market=market,
                        token_id=yes_token_id,
                        outcome="YES",
                        best_ask=best_ask,
                        fee=fee,
                        net_edge=net_edge,
                        yes_mid=yes_mid,
                        hours_left=hours_left,
                        market_type=market_type,
                        max_spend=max_spend,
                    )

        # --- NO side (YES is very unlikely) ---
        if yes_mid <= (1.0 - min_conviction):
            if no_book is not None and no_book.best_ask is not None:
                best_ask = no_book.best_ask
                if best_ask < 1.0:
                    # Use the NO ask price for fee calculation (price of NO = 1 - yes_mid approx)
                    no_mid = 1.0 - yes_mid
                    fee = calc_taker_fee(best_ask, market_type)
                    net_edge = (1.0 - best_ask) - fee
                    if net_edge >= min_edge:
                        return self._make_signal(
                            market=market,
                            token_id=no_token_id,
                            outcome="NO",
                            best_ask=best_ask,
                            fee=fee,
                            net_edge=net_edge,
                            yes_mid=yes_mid,
                            hours_left=hours_left,
                            market_type=market_type,
                            max_spend=max_spend,
                        )

        return None

    def _make_signal(
        self,
        market: Market,
        token_id: str,
        outcome: str,
        best_ask: float,
        fee: float,
        net_edge: float,
        yes_mid: float,
        hours_left: float,
        market_type: str,
        max_spend: float,
    ) -> Signal | None:
        arb_opportunities.labels(strategy="quick_resolution").inc()
        edge_detected.labels(strategy="quick_resolution").observe(net_edge)

        size_usdc = self.risk.size_position(edge=net_edge, base_size=max_spend)
        if size_usdc < MIN_TRADE_USDC:
            return None

        self._entered[market.condition_id] = (best_ask, time.time())

        self.log(
            f"[QUICK RESOLUTION] BUY {outcome} @ {best_ask:.4f} | "
            f"{hours_left:.2f}h left | yes_mid={yes_mid:.3f} | "
            f"fee={fee:.4f} ({market_type}) | edge={net_edge:.4f} | "
            f"size=${size_usdc:.2f} | {market.question[:60]}"
        )

        return Signal(
            strategy="quick_resolution",
            token_id=token_id,
            side="BUY",
            price=best_ask,
            size_usdc=size_usdc,
            edge=net_edge,
            notes=(
                f"[QUICK_RES] BUY {outcome} @ {best_ask:.4f} | "
                f"{hours_left:.2f}h | fee={fee:.4f} ({market_type}) | edge={net_edge:.4f}"
            ),
            metadata={
                "outcome": outcome,
                "yes_mid": yes_mid,
                "hours_left": hours_left,
                "market_type": market_type,
                "fee": fee,
                "net_edge": net_edge,
                "condition_id": market.condition_id,
                "entered_at": time.time(),
            },
        )
