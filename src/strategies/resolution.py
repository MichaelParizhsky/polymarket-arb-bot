"""
Strategy: Resolution Trading.

Near-expiry markets that are strongly trending toward YES or NO resolution
can offer near-guaranteed returns when the ask is still slightly below $1.00.

Logic:
  - Scan markets whose end_date_iso is within 48 hours.
  - If YES price is 0.80–0.97 AND market expires within 24 h AND best_ask < 0.96,
    buy YES.  Net edge = (1.00 - ask) - fees.  Minimum net edge: 2%.
  - If YES price is 0.03–0.20 (i.e. NO price is 0.80–0.97) AND market expires
    within 24 h AND NO best_ask < 0.96, buy NO.
  - Only enter once per market (tracked in self._resolution_positions).
  - Position sizing via risk_manager.size_position() with a bespoke base_size.
"""
from __future__ import annotations

import time
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.strategies.latency_arb import _days_to_expiry
from src.utils.constants import FEE_RATE
from src.utils.metrics import arb_opportunities, edge_detected

# --- Tier 1: Endgame Sweep (high-conviction, near-certain) ---
# Price >0.97 means outcome is nearly certain. <4h window = settlement imminent.
# Returns are small (0.3–2%) but very reliable. Less competition than arb.
ENDGAME_YES_LOW = 0.970     # YES must be at least this (strong conviction)
ENDGAME_YES_HIGH = 0.999    # YES must be below this (not already at payout)
ENDGAME_ASK_MAX = 0.998     # only buy if ask < $0.998 (at least 0.2c left)
ENDGAME_NO_LOW = 0.001      # YES price at most this for NO endgame entry
ENDGAME_NO_HIGH = 0.030     # YES price at most this for NO endgame entry
ENDGAME_HOURS = 4.0         # only within 4 hours of resolution (keep for backward compat)
ENDGAME_MIN_EDGE = 0.002    # minimum 0.2c net edge (fees are 0.2%, target >0.4% gross)

# --- Tier 2: Near-term resolution (moderate conviction) ---
# Price 0.90–0.97 with <12h remaining. Higher edge required to justify uncertainty.
YES_LIKELY_LOW = 0.90       # raised from 0.80 — avoids uncertain markets
YES_LIKELY_HIGH = 0.970     # capped below endgame range
YES_ASK_MAX = 0.96          # we only buy if ask is below this
NO_LIKELY_LOW = 0.030       # YES price at most this (NO strongly likely)
NO_LIKELY_HIGH = 0.10       # YES price at most this (tightened from 0.20)
NO_ASK_MAX = 0.96

MAX_HOURS_TO_SCAN = 48.0        # Scan up to 48h out; edge requirements scale with time
HOURS_THRESHOLD_INNER = 12.0   # Tier 2 only within 12h
HOURS_THRESHOLD_ENDGAME = 4.0  # Tier 1 only within 4h (alias for ENDGAME_HOURS)

# Market quality filters — only trade resolution strategy on liquid markets
MIN_VOLUME_24H = 500.0  # Lowered from 1000 — sports markets have $200-800 daily volume
MIN_LIQUIDITY_DEPTH = 100.0  # USDC at best ask required

# Category-specific edge requirements (higher for noisy categories)
CATEGORY_EDGE_MULTIPLIER = {
    "sports": 1.5,    # Sports have high information asymmetry
    "crypto": 1.2,
    "politics": 1.0,  # Standard
    "other": 1.0,
}


def _required_edge(hours_left: float) -> float:
    """Edge requirement scales down with time — less time = less adverse-move risk."""
    if hours_left <= 1.0:
        return 0.015   # <1h: 1.5% sufficient
    elif hours_left <= 4.0:
        return 0.020   # <4h: 2%
    elif hours_left <= 12.0:
        return 0.025   # <12h: 2.5%
    else:
        return 0.030   # >12h: 3%


# ------------------------------------------------------------------ #
#  Strategy                                                            #
# ------------------------------------------------------------------ #

class ResolutionStrategy(BaseStrategy):
    """
    Near-expiry resolution trading strategy.

    Buys YES (or NO) on markets that are strongly trending toward a
    resolution outcome with less than 24 hours remaining, provided the
    ask price is low enough to guarantee at least MIN_NET_EDGE after fees.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        # condition_id -> (entry_price, entered_at); prevents doubling up on the same market
        self._resolution_positions: dict[str, tuple[float, float]] = {}

    # ---------------------------------------------------------------- #
    #  Main scan                                                         #
    # ---------------------------------------------------------------- #

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        signals: list[Signal] = []

        # Diagnostic counters
        n_expired = 0
        n_volume = 0
        n_no_edge = 0
        markets_scanned = 0

        # Prune resolution positions older than 48h (markets long since resolved)
        cutoff = time.time() - 48 * 3600
        stale = [cid for cid, (_, entered_at) in self._resolution_positions.items() if entered_at < cutoff]
        for cid in stale:
            del self._resolution_positions[cid]

        for market in markets:
            if not market.active or market.closed:
                continue

            dte_days = _days_to_expiry(market.end_date_iso)
            hours_left = dte_days * 24.0

            # Only scan markets resolving within MAX_HOURS_TO_SCAN (48h)
            if hours_left > MAX_HOURS_TO_SCAN or hours_left <= 0:
                n_expired += 1
                continue

            # Skip illiquid markets
            volume = market.volume or 0.0
            if volume < MIN_VOLUME_24H:
                self.log(
                    f"Resolution: skipping {market.question[:50]} — volume ${volume:.0f} < ${MIN_VOLUME_24H:.0f}",
                    "debug",
                )
                n_volume += 1
                continue

            # Skip if we already have a resolution position on this market
            if market.condition_id in self._resolution_positions:
                continue

            markets_scanned += 1

            # Apply category-specific edge multiplier
            category = getattr(market, 'category', 'other').lower()
            edge_multiplier = CATEGORY_EDGE_MULTIPLIER.get(category, 1.0)

            sig = None

            # Tier 1: Endgame sweep (<4h, high confidence)
            if hours_left <= ENDGAME_HOURS:
                sig = self._evaluate_endgame(market, orderbooks, hours_left, edge_multiplier)
                if sig is not None:
                    signals.append(sig)
                    continue  # don't double-signal same market
                # Tier 1 failed — cascade to Tier 2 (e.g. 92% conviction with 30 min left)
                if hours_left <= HOURS_THRESHOLD_INNER:
                    sig = self._evaluate_market(market, orderbooks, hours_left, edge_multiplier)

            # Tier 2 only: <12h but not in endgame window
            elif hours_left <= HOURS_THRESHOLD_INNER:
                sig = self._evaluate_market(market, orderbooks, hours_left, edge_multiplier)

            if sig is not None:
                signals.append(sig)
            else:
                n_no_edge += 1

        self.log(
            f"Resolution scan: {markets_scanned} markets checked → "
            f"{len(signals)} signals | "
            f"skipped: {n_expired} out-of-window, {n_volume} low-volume, {n_no_edge} no-edge"
        )
        return signals

    def _evaluate_endgame(
        self,
        market: Market,
        orderbooks: dict[str, Orderbook],
        hours_left: float,
        edge_multiplier: float = 1.0,
    ) -> Signal | None:
        """
        Tier 1 — Endgame sweep: buy near-certain YES/NO within 4h of resolution.
        Smaller but very reliable returns ($0.002–$0.03 per contract).
        """
        yes_token = next(
            (t for t in market.tokens if t.outcome.lower() == "yes"),
            market.tokens[0] if market.tokens else None,
        )
        no_token = next(
            (t for t in market.tokens if t.outcome.lower() == "no"),
            market.tokens[1] if len(market.tokens) > 1 else None,
        )
        if not yes_token or not no_token:
            return None

        yes_book = orderbooks.get(yes_token.token_id)
        if not yes_book or yes_book.mid is None:
            return None
        yes_mid = yes_book.mid

        # YES endgame sweep
        if ENDGAME_YES_LOW <= yes_mid <= ENDGAME_YES_HIGH:
            best_ask = yes_book.best_ask
            if best_ask is None or best_ask >= ENDGAME_ASK_MAX:
                return None
            net_edge = (1.0 - best_ask) - FEE_RATE
            required_edge = ENDGAME_MIN_EDGE * edge_multiplier
            if net_edge < required_edge:
                return None

            arb_opportunities.labels(strategy="resolution").inc()
            edge_detected.labels(strategy="resolution").observe(net_edge)
            base_size = getattr(self.config.strategies, "rebalancing_max_spend", 50.0) / 2.0
            size_usdc = self.risk.size_position(edge=net_edge, base_size=base_size)
            if size_usdc < 1.0:
                return None

            self.log(
                f"[ENDGAME SWEEP] BUY YES @ {best_ask:.4f} | "
                f"{hours_left:.1f}h left | yes_mid={yes_mid:.3f} | "
                f"edge={net_edge:.4f} | size=${size_usdc:.2f} | {market.question[:50]}"
            )
            self._resolution_positions[market.condition_id] = (best_ask, time.time())
            return Signal(
                strategy="resolution",
                token_id=yes_token.token_id,
                side="BUY",
                price=best_ask,
                size_usdc=size_usdc,
                edge=net_edge,
                notes=f"[ENDGAME SWEEP] YES @ {best_ask:.4f} | {hours_left:.1f}h",
                metadata={"tier": "endgame", "outcome": "YES", "hours_left": hours_left,
                          "yes_mid": yes_mid, "net_edge": net_edge},
            )

        # NO endgame sweep
        if ENDGAME_NO_LOW <= yes_mid <= ENDGAME_NO_HIGH:
            no_book = orderbooks.get(no_token.token_id)
            if not no_book or no_book.mid is None or no_book.best_ask is None:
                return None
            if no_book.best_ask >= ENDGAME_ASK_MAX:
                return None
            net_edge = (1.0 - no_book.best_ask) - FEE_RATE
            required_edge = ENDGAME_MIN_EDGE * edge_multiplier
            if net_edge < required_edge:
                return None

            arb_opportunities.labels(strategy="resolution").inc()
            edge_detected.labels(strategy="resolution").observe(net_edge)
            base_size = getattr(self.config.strategies, "rebalancing_max_spend", 50.0) / 2.0
            size_usdc = self.risk.size_position(edge=net_edge, base_size=base_size)
            if size_usdc < 1.0:
                return None

            self.log(
                f"[ENDGAME SWEEP] BUY NO @ {no_book.best_ask:.4f} | "
                f"{hours_left:.1f}h left | yes_mid={yes_mid:.3f} | "
                f"edge={net_edge:.4f} | size=${size_usdc:.2f} | {market.question[:50]}"
            )
            self._resolution_positions[market.condition_id] = (no_book.best_ask, time.time())
            return Signal(
                strategy="resolution",
                token_id=no_token.token_id,
                side="BUY",
                price=no_book.best_ask,
                size_usdc=size_usdc,
                edge=net_edge,
                notes=f"[ENDGAME SWEEP] NO @ {no_book.best_ask:.4f} | {hours_left:.1f}h",
                metadata={"tier": "endgame", "outcome": "NO", "hours_left": hours_left,
                          "yes_mid": yes_mid, "net_edge": net_edge},
            )

        return None

    # ---------------------------------------------------------------- #
    #  Per-market evaluation                                             #
    # ---------------------------------------------------------------- #

    def _evaluate_market(
        self,
        market: Market,
        orderbooks: dict[str, Orderbook],
        hours_left: float,
        edge_multiplier: float = 1.0,
    ) -> Signal | None:
        """
        Evaluate a single market for a resolution trade.
        Returns a Signal if an actionable opportunity exists, else None.
        """
        yes_token = next(
            (t for t in market.tokens if t.outcome.lower() == "yes"),
            market.tokens[0] if market.tokens else None,
        )
        no_token = next(
            (t for t in market.tokens if t.outcome.lower() == "no"),
            market.tokens[1] if len(market.tokens) > 1 else None,
        )
        if not yes_token or not no_token:
            return None

        yes_book = orderbooks.get(yes_token.token_id)
        no_book = orderbooks.get(no_token.token_id)

        # Need at least the YES mid to assess probability
        if not yes_book or yes_book.mid is None:
            return None

        yes_mid = yes_book.mid

        # --- YES resolution path ---
        if YES_LIKELY_LOW <= yes_mid <= YES_LIKELY_HIGH:
            return self._check_yes_entry(
                market=market,
                yes_token_id=yes_token.token_id,
                yes_book=yes_book,
                yes_mid=yes_mid,
                hours_left=hours_left,
                edge_multiplier=edge_multiplier,
            )

        # --- NO resolution path ---
        if NO_LIKELY_LOW <= yes_mid <= NO_LIKELY_HIGH:
            if no_book is None:
                return None
            no_mid = no_book.mid
            if no_mid is None:
                return None
            return self._check_no_entry(
                market=market,
                no_token_id=no_token.token_id,
                no_book=no_book,
                no_mid=no_mid,
                yes_mid=yes_mid,
                hours_left=hours_left,
                edge_multiplier=edge_multiplier,
            )

        return None

    # ---------------------------------------------------------------- #
    #  YES entry check                                                   #
    # ---------------------------------------------------------------- #

    def _check_yes_entry(
        self,
        market: Market,
        yes_token_id: str,
        yes_book: Orderbook,
        yes_mid: float,
        hours_left: float,
        edge_multiplier: float = 1.0,
    ) -> Signal | None:
        best_ask = yes_book.best_ask
        if best_ask is None:
            return None
        if best_ask >= YES_ASK_MAX:
            return None

        # Net edge = (resolution payout $1.00) – ask – fee
        gross_edge = 1.0 - best_ask
        net_edge = gross_edge - FEE_RATE

        required_edge = _required_edge(hours_left) * edge_multiplier
        if net_edge < required_edge:
            return None

        arb_opportunities.labels(strategy="resolution").inc()
        edge_detected.labels(strategy="resolution").observe(net_edge)

        base_size = getattr(
            self.config.strategies, "rebalancing_max_spend", 50.0
        ) / 2.0
        size_usdc = self.risk.size_position(edge=net_edge, base_size=base_size)
        if size_usdc < 1.0:
            return None

        self.log(
            f"[RESOLUTION] BUY YES @ {best_ask:.4f} | "
            f"market resolves in {hours_left:.1f}h | "
            f"yes_mid={yes_mid:.3f} | edge={net_edge:.3f} | "
            f"size=${size_usdc:.2f} | q={market.question[:60]}"
        )

        self._resolution_positions[market.condition_id] = (best_ask, time.time())

        return Signal(
            strategy="resolution",
            token_id=yes_token_id,
            side="BUY",
            price=best_ask,
            size_usdc=size_usdc,
            edge=net_edge,
            notes=(
                f"[RESOLUTION] BUY YES @ {best_ask:.4f} | "
                f"market resolves in {hours_left:.1f}h | "
                f"edge={net_edge:.3f}"
            ),
            metadata={
                "strategy": "resolution",
                "outcome": "YES",
                "yes_mid": yes_mid,
                "hours_left": hours_left,
                "gross_edge": gross_edge,
                "net_edge": net_edge,
                "condition_id": market.condition_id,
                "entered_at": time.time(),
            },
        )

    # ---------------------------------------------------------------- #
    #  NO entry check                                                    #
    # ---------------------------------------------------------------- #

    def _check_no_entry(
        self,
        market: Market,
        no_token_id: str,
        no_book: Orderbook,
        no_mid: float,
        yes_mid: float,
        hours_left: float,
        edge_multiplier: float = 1.0,
    ) -> Signal | None:
        best_ask = no_book.best_ask
        if best_ask is None:
            return None
        if best_ask >= NO_ASK_MAX:
            return None

        # Net edge = (resolution payout $1.00) – ask – fee
        gross_edge = 1.0 - best_ask
        net_edge = gross_edge - FEE_RATE

        required_edge = _required_edge(hours_left) * edge_multiplier
        if net_edge < required_edge:
            return None

        arb_opportunities.labels(strategy="resolution").inc()
        edge_detected.labels(strategy="resolution").observe(net_edge)

        base_size = getattr(
            self.config.strategies, "rebalancing_max_spend", 50.0
        ) / 2.0
        size_usdc = self.risk.size_position(edge=net_edge, base_size=base_size)
        if size_usdc < 1.0:
            return None

        self.log(
            f"[RESOLUTION] BUY NO @ {best_ask:.4f} | "
            f"market resolves in {hours_left:.1f}h | "
            f"yes_mid={yes_mid:.3f} (NO likely) | edge={net_edge:.3f} | "
            f"size=${size_usdc:.2f} | q={market.question[:60]}"
        )

        self._resolution_positions[market.condition_id] = (best_ask, time.time())

        return Signal(
            strategy="resolution",
            token_id=no_token_id,
            side="BUY",
            price=best_ask,
            size_usdc=size_usdc,
            edge=net_edge,
            notes=(
                f"[RESOLUTION] BUY NO @ {best_ask:.4f} | "
                f"market resolves in {hours_left:.1f}h | "
                f"edge={net_edge:.3f}"
            ),
            metadata={
                "strategy": "resolution",
                "outcome": "NO",
                "yes_mid": yes_mid,
                "no_mid": no_mid,
                "hours_left": hours_left,
                "gross_edge": gross_edge,
                "net_edge": net_edge,
                "condition_id": market.condition_id,
                "entered_at": time.time(),
            },
        )
