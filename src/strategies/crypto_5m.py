"""
Strategy: Crypto Short Market Arb — 5-minute and 15-minute up/down markets.

Three entry modes:

1. DUAL-SIDE ARB (preferred):
   If YES_ask + NO_ask < DUAL_ARB_THRESHOLD (0.995), buy BOTH sides.
   One must resolve at $1.00, so guaranteed profit regardless of direction.
   Edge = 1.0 - (YES_ask + NO_ask) - fees

2. END-OF-WINDOW SNIPE:
   Within 60 seconds of window close, if one side >80% mid, buy that side.
   Price direction is already telegraphed by momentum at T-60s.
   Grok X sentiment is checked before firing — contradicting signals are skipped.

3. ORACLE LAG DISLOCATION (new):
   Chainlink oracle updates BTC/USD every ~10-30s or on 0.5% price deviations.
   When BTC has moved significantly on Binance but the Polymarket token price
   hasn't repriced yet, we have a 15-45 second window to trade the known direction.
   Uses BinanceFeed (already in context) — no new dependencies.
   Gated by 10-minute medium-term trend to avoid fighting the macro direction.

Why this works:
  - Markets rotate every 5 or 15 minutes — capital recycles extremely fast
  - Dual-side arb is market-neutral: no prediction skill required
  - End-of-window entries capture the final price certainty premium
  - Oracle lag exploits the Chainlink update delay with a known direction
  - These are the highest-frequency markets on Polymarket
"""
from __future__ import annotations

import re as _re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from src.exchange.polymarket import Market, Orderbook, Token
from src.strategies.base import BaseStrategy, Signal
from src.utils.ai_research import grok
from src.utils.constants import MIN_TRADE_USDC
from src.utils.logger import logger
from src.utils.metrics import arb_opportunities, edge_detected

# Combined YES+NO ask must be below this for dual-side arb
DUAL_ARB_THRESHOLD = 0.995

# End-of-window snipe: fire when this many seconds remain in the window
SNIPE_WINDOW_SECONDS = 120.0

# Minimum conviction for end-of-window snipe
SNIPE_MIN_CONVICTION = 0.65

# Minimum net edge required for any entry
MIN_NET_EDGE = 0.005  # 0.5%

# Polymarket taker fee (standard markets, 2025 rate — near zero)
TAKER_FEE = 0.002

# ── Oracle lag constants (Mode 3) ────────────────────────────────────────────
# BTC must move at least this much in the look-back window to fire a signal
ORACLE_LAG_MIN_MOVE_PCT_DEFAULT = 0.0005   # 0.05% — overridden by config
# Minimum fee-adjusted net edge to fire
ORACLE_LAG_MIN_EDGE_DEFAULT = 0.022        # 2.2%  — overridden by config
# How far back to measure BTC momentum (seconds)
ORACLE_LAG_LOOKBACK_S = 30.0
# Medium-term trend window for gating (10 minutes)
MEDIUM_TREND_WINDOW_S = 600
# Only fire oracle lag signals this far from window end (avoid last-second chaos)
ORACLE_LAG_MAX_SECONDS_LEFT = 240.0
# ─────────────────────────────────────────────────────────────────────────────

# Regex to extract crypto symbol from a market question
_SYMBOL_RE = _re.compile(
    r'\b(BTC|ETH|SOL|BNB|DOGE|ADA|XRP|AVAX|MATIC|LINK|DOT|UNI|ATOM|LTC|BCH)\b',
    _re.IGNORECASE,
)


def _extract_crypto_symbol(question: str) -> str | None:
    """Extract a known crypto ticker from a market question string."""
    m = _SYMBOL_RE.search(question)
    return m.group(1).upper() if m else None


def _seconds_to_window_close(
    end_date_iso: str, now_utc: datetime | None = None
) -> float | None:
    """Return seconds until this market's window closes, or None if unparseable."""
    try:
        now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        s = end_date_iso.strip().rstrip("Z")
        if "T" not in s:
            s += "T00:00:00"
        end = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return (end - now).total_seconds()
    except Exception:
        return None


def _compute_move_pct(price_history: deque, window_s: float) -> float | None:
    """
    Compute signed % move over the last window_s seconds from a deque of
    (price, timestamp) tuples. Returns None if not enough data.
    """
    now = time.time()
    cutoff = now - window_s
    old_ticks = [(p, t) for p, t in price_history if t >= cutoff]
    return _move_pct_from_ticks(old_ticks)


def _move_pct_from_ticks(ticks: list[tuple[float, float]]) -> float | None:
    if len(ticks) < 2:
        return None
    oldest_price = ticks[0][0]
    newest_price = ticks[-1][0]
    if oldest_price <= 0:
        return None
    return (newest_price - oldest_price) / oldest_price


def _compute_medium_trend(price_history: deque, window_s: float = MEDIUM_TREND_WINDOW_S) -> str | None:
    """
    Linear regression slope direction over the last window_s seconds.
    Returns 'UP', 'DOWN', or None if insufficient data.
    """
    now = time.time()
    cutoff = now - window_s
    ticks = [(p, t) for p, t in price_history if t >= cutoff]
    return _medium_trend_from_ticks(ticks)


def _medium_trend_from_ticks(ticks: list[tuple[float, float]]) -> str | None:
    if len(ticks) < 10:
        return None
    n = len(ticks)
    xs = list(range(n))
    ys = [p for p, _ in ticks]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n)) or 1e-9
    slope = num / den
    return "UP" if slope > 0 else "DOWN"


def _btc_ticks_for_oracle(
    price_history: deque, now: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """
    Single pass over BTC history: ticks for 30s move vs 600s trend windows.
    Avoids duplicating full deque scans each scan cycle.
    """
    cu30 = now - ORACLE_LAG_LOOKBACK_S
    cu600 = now - MEDIUM_TREND_WINDOW_S
    t30: list[tuple[float, float]] = []
    t600: list[tuple[float, float]] = []
    for p, t in price_history:
        if t < cu600:
            continue
        t600.append((p, t))
        if t >= cu30:
            t30.append((p, t))
    return t30, t600


def _resolve_yes_no_tokens(market: Market) -> tuple[Token | None, Token | None]:
    """Match Polymarket crypto short token naming (yes/no/up/down) in ≤2 passes."""
    yes_token = no_token = None
    for t in market.tokens:
        ol = t.outcome.lower()
        if ol == "yes":
            yes_token = t
        elif ol in ("no", "down"):
            no_token = t
    if yes_token and no_token:
        return yes_token, no_token
    yes_token = no_token = None
    for t in market.tokens:
        ol = t.outcome.lower()
        if ol in ("yes", "up") and not yes_token:
            yes_token = t
        elif ol in ("no", "down") and not no_token:
            no_token = t
    return yes_token, no_token


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
        self._grok_status_logged = False
        # Oracle lag: per-symbol price history deque (price, timestamp)
        # Max 1200 entries = ~20 min of 1s ticks
        self._btc_price_history: deque = deque(maxlen=1200)
        self._oracle_lag_status_logged = False

    def _update_btc_history(self, binance_feed) -> None:
        """Append current BTC price from BinanceFeed to the local history deque."""
        if binance_feed is None:
            return
        tick = binance_feed.get_price("BTCUSDT")
        if tick and not binance_feed.is_stale("BTCUSDT", max_age_seconds=5.0):
            self._btc_price_history.append((tick.price, time.time()))

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        # This strategy gets its own market list from the context key
        # populated by main.py calling get_crypto_short_markets()
        markets: list[Market] = context.get("crypto_short_markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})

        if not markets:
            logger.debug("CryptoShort: no crypto_short_markets in context — skipping scan")
            return []

        now_utc = datetime.now(timezone.utc)

        # Log Grok status once at startup
        if not self._grok_status_logged:
            self.log(f"CryptoShort: Grok X sentiment {'enabled' if grok.enabled else 'disabled (no GROK_API_KEY)'}")
            self._grok_status_logged = True

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

        # ── Oracle lag: update BTC price history every scan cycle ─────────
        if _binance_live:
            self._update_btc_history(binance_feed)
            if not self._oracle_lag_status_logged:
                self.log("CryptoShort: Oracle lag mode (Mode 3) active — tracking BTC price history")
                self._oracle_lag_status_logged = True
        # ──────────────────────────────────────────────────────────────────

        if not _binance_live:
            self.log(
                "Binance feed stale or unavailable — snipe + oracle lag modes disabled (no directional edge without price reference)",
                "warning",
            )
        _snipe_allowed = _binance_live
        _oracle_lag_allowed = _binance_live and len(self._btc_price_history) >= 10

        # Load oracle lag thresholds from config (meta-agent can tune these live)
        cfg = self.config.strategies
        oracle_min_move = getattr(cfg, "oracle_lag_min_move_pct", ORACLE_LAG_MIN_MOVE_PCT_DEFAULT)
        oracle_min_edge = getattr(cfg, "oracle_lag_min_edge", ORACLE_LAG_MIN_EDGE_DEFAULT)

        signals: list[Signal] = []
        max_spend = getattr(cfg, "crypto_5m_max_spend", 100.0)

        # Prune stale entries (windows are 5-15 min, keep 30min buffer)
        cutoff = time.time() - 1800
        self._entered = {k: v for k, v in self._entered.items() if v > cutoff}

        # Pre-compute oracle lag signal (once per scan, shared across markets)
        _oracle_direction: str | None = None
        _oracle_move_pct: float = 0.0
        _oracle_fee_adjusted_edge: float = 0.0
        _oracle_confidence: str = "LOW"
        _medium_trend: str | None = None

        if _oracle_lag_allowed:
            _now_ts = time.time()
            _ticks30, _ticks600 = _btc_ticks_for_oracle(self._btc_price_history, _now_ts)
            move_pct = _move_pct_from_ticks(_ticks30)
            _medium_trend = _medium_trend_from_ticks(_ticks600)

            if move_pct is not None and abs(move_pct) >= oracle_min_move:
                _oracle_move_pct = move_pct
                _oracle_direction = "YES" if move_pct > 0 else "NO"

                # Gating: block signals opposing the 10-minute trend
                if _medium_trend is not None:
                    trend_consistent = (
                        (_oracle_direction == "YES" and _medium_trend == "UP") or
                        (_oracle_direction == "NO" and _medium_trend == "DOWN")
                    )
                    if not trend_consistent:
                        self.log(
                            f"[ORACLE LAG] Signal {_oracle_direction} blocked: "
                            f"opposes 10-min trend {_medium_trend} (move={move_pct:.4%})",
                            "debug",
                        )
                        _oracle_direction = None  # gate: don't fire against macro trend

                if _oracle_direction:
                    # Confidence tier based on move magnitude
                    if abs(move_pct) > 0.002:     # >0.2% move
                        _oracle_confidence = "HIGH"
                    elif abs(move_pct) > 0.001:   # >0.1% move
                        _oracle_confidence = "MEDIUM"
                    else:
                        _oracle_confidence = "LOW"

        for market in markets:
            if not market.active or market.closed:
                continue
            if market.condition_id in self._entered:
                continue

            yes_token, no_token = _resolve_yes_no_tokens(market)
            if not yes_token or not no_token:
                continue

            symbol_q = _extract_crypto_symbol(market.question)

            yes_book = orderbooks.get(yes_token.token_id)
            no_book  = orderbooks.get(no_token.token_id)

            if not yes_book or not no_book:
                continue

            yes_ask = yes_book.best_ask
            no_ask  = no_book.best_ask
            yes_mid = yes_book.mid

            if yes_ask is None or no_ask is None:
                continue

            seconds_left = _seconds_to_window_close(market.end_date_iso, now_utc)
            if seconds_left is None or seconds_left <= 0:
                continue

            # ── Mode 1: Dual-side guaranteed arb ─────────────────────────
            dual_thr = getattr(cfg, "crypto_5m_dual_arb_threshold", DUAL_ARB_THRESHOLD)
            min_net = getattr(cfg, "crypto_5m_min_net_edge", MIN_NET_EDGE)
            combined_ask = yes_ask + no_ask
            if combined_ask < dual_thr:
                gross_edge = 1.0 - combined_ask
                net_edge   = gross_edge - (TAKER_FEE * 2)  # fee on both legs
                if net_edge >= min_net:
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
                                "pair_token_id": no_token.token_id,
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
                                "pair_token_id": yes_token.token_id,
                                "combined_ask": combined_ask,
                                "net_edge": net_edge,
                                "seconds_left": seconds_left,
                                "condition_id": market.condition_id,
                            },
                        ))
                    continue  # don't also try snipe or oracle lag mode

            # ── Mode 3: Oracle lag dislocation ───────────────────────────
            # Check before snipe: oracle lag fires earlier in the window;
            # snipe fires only in the last SNIPE_WINDOW_SECONDS seconds.
            # This avoids double-entering the same market.
            if (
                _oracle_lag_allowed
                and _oracle_direction is not None
                and seconds_left > SNIPE_WINDOW_SECONDS  # not in snipe zone
                and seconds_left <= ORACLE_LAG_MAX_SECONDS_LEFT  # not too early
            ):
                # Only fire on BTC markets (oracle lag is Chainlink BTC/USD specific)
                if symbol_q == "BTC":
                    if _oracle_direction == "YES":
                        entry_price = yes_ask
                        token = yes_token
                        outcome = "YES"
                    else:
                        entry_price = no_ask
                        token = no_token
                        outcome = "NO"

                    if entry_price is not None and entry_price < 1.0:
                        # net_edge: gross payout (1.0 - entry) minus single taker fee on entry.
                        # Resolution pays $1.00 with no exit fee, so only one fee applies.
                        net_edge = (1.0 - entry_price) - TAKER_FEE

                        if net_edge >= oracle_min_edge:
                            arb_opportunities.labels(strategy="crypto_5m").inc()
                            edge_detected.labels(strategy="crypto_5m").observe(net_edge)

                            # Size using confidence tier: HIGH=full Kelly, MED=0.6x, LOW=0.3x
                            confidence_scale = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}.get(
                                _oracle_confidence, 0.5
                            )
                            raw_size = self.risk.size_position(edge=net_edge, base_size=max_spend)
                            size_usdc = max(MIN_TRADE_USDC, raw_size * confidence_scale)

                            if size_usdc >= MIN_TRADE_USDC:
                                self._entered[market.condition_id] = time.time()
                                self.log(
                                    f"[ORACLE LAG] {outcome} @ {entry_price:.4f} | "
                                    f"btc_move={_oracle_move_pct:.4%} edge={net_edge:.4f} "
                                    f"conf={_oracle_confidence} trend={_medium_trend} | "
                                    f"{seconds_left:.0f}s left | {market.question[:50]}"
                                )
                                signals.append(Signal(
                                    strategy="crypto_5m",
                                    token_id=token.token_id,
                                    side="BUY",
                                    price=entry_price,
                                    size_usdc=size_usdc,
                                    edge=net_edge,
                                    notes=(
                                        f"[ORACLE_LAG] {outcome} | "
                                        f"btc_move={_oracle_move_pct:.4%} "
                                        f"conf={_oracle_confidence}"
                                    ),
                                    metadata={
                                        "outcome": outcome,
                                        "arb_type": "oracle_lag",
                                        "btc_move_pct": _oracle_move_pct,
                                        "fee_adjusted_edge": net_edge,
                                        "seconds_left": seconds_left,
                                        "confidence": _oracle_confidence,
                                        "medium_trend": _medium_trend,
                                        "condition_id": market.condition_id,
                                    },
                                ))
                    continue  # don't double-enter via snipe

            # ── Mode 2: End-of-window momentum snipe ─────────────────────
            if not _snipe_allowed:
                continue  # no Binance data = no directional edge = skip snipe
            if seconds_left <= SNIPE_WINDOW_SECONDS and yes_mid is not None:
                if yes_mid >= SNIPE_MIN_CONVICTION:
                    net_edge = (1.0 - yes_ask) - TAKER_FEE
                    if net_edge >= MIN_NET_EDGE and yes_ask < 1.0:
                        # ── Grok sentiment gate (YES snipe) ──────────────
                        grok_direction: str | None = None
                        grok_strength: float | None = None
                        if symbol_q and grok.enabled:
                            try:
                                momentum = await grok.get_crypto_momentum(symbol_q)
                                grok_direction = momentum.get("direction", "sideways")
                                grok_strength = momentum.get("strength", 0.0)

                                if grok_strength >= 0.5 and grok_direction == "up":
                                    # Grok confirms YES snipe direction
                                    self.log(
                                        f"[GROK CONFIRM] {symbol_q} momentum={grok_direction} "
                                        f"strength={grok_strength:.2f}"
                                    )
                                elif grok_strength >= 0.5 and grok_direction not in ("up", "sideways"):
                                    # Grok contradicts — skip this snipe
                                    self.log(
                                        f"[GROK BLOCK] snipe skipped — {symbol_q} Grok says "
                                        f"{grok_direction} (strength={grok_strength:.2f})",
                                        "debug",
                                    )
                                    continue  # skip to next market
                                # else: Grok unclear (strength < 0.5 or sideways) — proceed
                            except Exception as exc:
                                self.log(f"[GROK WARN] get_crypto_momentum failed for {symbol_q}: {exc}", "warning")
                                # Do not block snipe on Grok error
                        # ─────────────────────────────────────────────────

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
                                    "grok_direction": grok_direction if symbol_q else None,
                                    "grok_strength": grok_strength if symbol_q else None,
                                },
                            ))

                elif yes_mid <= (1.0 - SNIPE_MIN_CONVICTION):
                    net_edge = (1.0 - no_ask) - TAKER_FEE
                    if net_edge >= MIN_NET_EDGE and no_ask < 1.0:
                        # ── Grok sentiment gate (NO snipe) ───────────────
                        grok_direction = None
                        grok_strength = None
                        if symbol_q and grok.enabled:
                            try:
                                momentum = await grok.get_crypto_momentum(symbol_q)
                                grok_direction = momentum.get("direction", "sideways")
                                grok_strength = momentum.get("strength", 0.0)

                                if grok_strength >= 0.5 and grok_direction == "down":
                                    # Grok confirms NO snipe direction
                                    self.log(
                                        f"[GROK CONFIRM] {symbol_q} momentum={grok_direction} "
                                        f"strength={grok_strength:.2f}"
                                    )
                                elif grok_strength >= 0.5 and grok_direction not in ("down", "sideways"):
                                    # Grok contradicts — skip this snipe
                                    self.log(
                                        f"[GROK BLOCK] snipe skipped — {symbol_q} Grok says "
                                        f"{grok_direction} (strength={grok_strength:.2f})",
                                        "debug",
                                    )
                                    continue  # skip to next market
                                # else: Grok unclear (strength < 0.5 or sideways) — proceed
                            except Exception as exc:
                                self.log(f"[GROK WARN] get_crypto_momentum failed for {symbol_q}: {exc}", "warning")
                                # Do not block snipe on Grok error
                        # ─────────────────────────────────────────────────

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
                                    "grok_direction": grok_direction if symbol_q else None,
                                    "grok_strength": grok_strength if symbol_q else None,
                                },
                            ))

        if signals:
            logger.info(f"CryptoShort: {len(signals)} signals generated")

        return signals
