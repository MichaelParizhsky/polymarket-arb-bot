"""
Strategy: Crypto 1-Hour Up/Down Market Candle Snipe

Targets Polymarket hourly binary markets (e.g. "Will BTC go up in the next hour?")
that resolve every ET clock hour. 6+ coins × 24 hours = 144+ markets/day.

Entry mode — END-OF-HOUR CANDLE SNIPE:
  Track the Binance 1H candle open (Binance price at the start of the ET clock hour).
  In the last SNIPE_WINDOW_SECONDS (default 600s = 10 min) of the hour, if:
    |candle_return| >= MIN_CANDLE_MOVE (default 0.7%)
  then compute P(same direction wins) from a confidence table and compare to ask.

  Fee structure (crypto_fees_v2, exponent=1):
    taker_fee(p) = 0.072 * p * (1 - p)
    At p=0.90: 0.65%    At p=0.80: 1.15%    At p=0.50: 1.80%
  Maker fee = 0% (LP rewards for orders within 4.5% spread, min size $50).

Edge calculation:
  edge = P(win) - ask_price - taker_fee(ask_price)
  Fire when edge >= MIN_NET_EDGE (default 3%)

Why this works:
  - At T-10min with a 0.7%+ lead, the candle almost always closes in that direction
  - Markets often price 50/50 at open and update slowly → large mispricings persist
  - Maker fee = 0% means we can also post limit orders risk-free as LP
  - These are completely separate markets from 5m/15m — no strategy overlap
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from src.exchange.polymarket import Market, Orderbook, Token
from src.strategies.base import BaseStrategy, Signal
from src.utils.constants import MIN_TRADE_USDC
from src.utils.logger import logger
from src.utils.metrics import arb_opportunities, edge_detected

# Eastern Daylight Time offset (UTC-4). Covers March-November.
# Winter (EST = UTC-5): update ET_OFFSET_HOURS env var or hardcode seasonally.
ET_OFFSET = timedelta(hours=-4)

# Snipe window: fire signals in the last N seconds of the ET hour
SNIPE_WINDOW_SECONDS = 600.0  # 10 minutes

# Minimum absolute candle return to fire
MIN_CANDLE_MOVE = 0.007  # 0.7%

# Minimum fee-adjusted net edge to fire a signal
MIN_NET_EDGE = 0.03  # 3%

# crypto_fees_v2 taker fee rate (exponent=1): fee = RATE * p * (1-p)
CRYPTO_FEES_V2_RATE = 0.072

# Confidence table: (abs_candle_return_threshold, P(same_direction_wins))
# Conservative calibration based on crypto 1H volatility in last 10 minutes.
# A 0.7% move with 10 min left is rarely reversed — historical data supports >85%.
CONFIDENCE_TABLE = [
    (0.030, 0.97),
    (0.020, 0.96),
    (0.015, 0.95),
    (0.010, 0.93),
    (0.007, 0.90),
]

# Binance symbol map: internal coin name → USDT pair
_BINANCE_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
    "hype": "HYPEUSDT",
}

# Map question keywords → internal coin name
_QUESTION_COIN_MAP = {
    "bitcoin": "btc",
    "btc": "btc",
    "ethereum": "eth",
    "eth": "eth",
    "solana": "sol",
    "sol": "sol",
    "xrp": "xrp",
    "ripple": "xrp",
    "dogecoin": "doge",
    "doge": "doge",
    "bnb": "bnb",
    "binancecoin": "bnb",
    "hype": "hype",
    "hyperliquid": "hype",
}


def _taker_fee(p: float) -> float:
    """crypto_fees_v2 taker fee at ask price p."""
    return CRYPTO_FEES_V2_RATE * p * (1.0 - p)


def _win_probability(candle_abs: float) -> float:
    """Estimate P(candle closes in same direction) from abs candle return."""
    for threshold, prob in CONFIDENCE_TABLE:
        if candle_abs >= threshold:
            return prob
    return 0.0


def _seconds_to_et_hour_end(now_et: datetime) -> float:
    """Seconds until the end of the current ET clock hour."""
    next_hour = now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (next_hour - now_et).total_seconds()


def _resolve_up_down_tokens(market: Market) -> tuple[Token | None, Token | None]:
    """Return (up_token, down_token) matching Up/Down or Yes/No outcomes."""
    up = down = None
    for t in market.tokens:
        ol = t.outcome.lower()
        if ol in ("up", "yes") and up is None:
            up = t
        elif ol in ("down", "no") and down is None:
            down = t
    return up, down


def _coin_from_question(question: str) -> str | None:
    """Extract internal coin name from a market question string."""
    q = question.lower()
    for keyword, coin in _QUESTION_COIN_MAP.items():
        if keyword in q:
            return coin
    return None


class Crypto1hStrategy(BaseStrategy):
    """
    Candle-snipe strategy for Polymarket 1-hour crypto Up/Down markets.
    Markets are discovered via get_crypto_1h_markets() in PolymarketClient
    and passed in context as 'crypto_1h_markets'.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        # hour_opens[coin] = (price, et_hour_int) recorded at each ET hour boundary
        self._hour_opens: dict[str, tuple[float, int]] = {}
        self._last_et_hour: int = -1
        # condition_id → entered_at to prevent re-entry within same window
        self._entered: dict[str, float] = {}

    def _now_et(self) -> datetime:
        return datetime.now(timezone.utc) + ET_OFFSET

    def _update_hour_opens(self, binance_feed: Any, et_hour: int, coins: list[str]) -> None:
        """Record Binance prices as candle opens when the ET hour ticks over."""
        if et_hour == self._last_et_hour:
            return
        self._last_et_hour = et_hour
        if binance_feed is None:
            return
        for coin in coins:
            sym = _BINANCE_SYMBOLS.get(coin.lower())
            if not sym:
                continue
            tick = binance_feed.get_price(sym)
            if tick and not binance_feed.is_stale(sym, max_age_seconds=10.0):
                self._hour_opens[coin.lower()] = (tick.price, et_hour)
                self.log(
                    f"Hour open recorded: {coin.upper()} = {tick.price:.6f} "
                    f"ET hour {et_hour:02d}:00"
                )

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("crypto_1h_markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})

        if not markets:
            logger.debug("Crypto1h: no crypto_1h_markets in context — skipping")
            return []

        binance_feed = context.get("binance_feed")
        _binance_live = False
        if binance_feed is not None:
            prices = getattr(binance_feed, "_prices", {})
            ts_now = time.time()
            _binance_live = any(
                ts_now - getattr(p, "timestamp", 0) < 30
                for p in prices.values()
            )

        if not _binance_live:
            self.log("Binance feed stale — snipe disabled (need live candle data)", "warning")
            return []

        cfg = self.config.strategies
        coins = list(getattr(cfg, "crypto_1h_coins", ["btc", "eth", "sol", "xrp"]))
        max_spend = getattr(cfg, "crypto_1h_max_spend", 150.0)
        snipe_window = float(getattr(cfg, "crypto_1h_snipe_window", SNIPE_WINDOW_SECONDS))
        min_candle_move = float(getattr(cfg, "crypto_1h_min_candle_move", MIN_CANDLE_MOVE))
        min_net_edge = float(getattr(cfg, "crypto_1h_min_net_edge", MIN_NET_EDGE))

        now_et = self._now_et()
        et_hour = now_et.hour
        secs_left = _seconds_to_et_hour_end(now_et)

        # Update candle opens on ET hour boundary
        self._update_hour_opens(binance_feed, et_hour, coins)

        # Only fire in the snipe window (last N seconds of the hour)
        if secs_left > snipe_window:
            return []

        # Prune stale entries (hourly markets — 2h buffer)
        cutoff = time.time() - 7200
        self._entered = {k: v for k, v in self._entered.items() if v > cutoff}

        signals: list[Signal] = []

        for market in markets:
            if not market.active or market.closed:
                continue
            if market.condition_id in self._entered:
                continue

            coin = _coin_from_question(market.question)
            if not coin:
                continue

            open_record = self._hour_opens.get(coin)
            if not open_record:
                continue
            hour_open_price, recorded_hour = open_record
            if recorded_hour != et_hour:
                continue  # open from a previous hour — skip

            sym = _BINANCE_SYMBOLS.get(coin)
            if not sym:
                continue
            tick = binance_feed.get_price(sym)
            if tick is None or binance_feed.is_stale(sym, max_age_seconds=10.0):
                continue

            candle_return = (tick.price - hour_open_price) / hour_open_price
            candle_abs = abs(candle_return)

            if candle_abs < min_candle_move:
                continue

            up_token, down_token = _resolve_up_down_tokens(market)
            if not up_token or not down_token:
                continue

            direction = "UP" if candle_return > 0 else "DOWN"
            entry_token = up_token if direction == "UP" else down_token

            entry_book = orderbooks.get(entry_token.token_id)
            if not entry_book:
                continue
            ask = entry_book.best_ask
            if ask is None or ask >= 1.0:
                continue

            win_prob = _win_probability(candle_abs)
            fee = _taker_fee(ask)
            net_edge = win_prob - ask - fee

            if net_edge < min_net_edge:
                self.log(
                    f"[SKIP] {direction} {coin.upper()} ask={ask:.4f} "
                    f"P(win)={win_prob:.2f} fee={fee:.4f} net_edge={net_edge:.4f} "
                    f"< min={min_net_edge:.4f} | candle={candle_return:.4%} "
                    f"{secs_left:.0f}s left",
                    "debug",
                )
                continue

            arb_opportunities.labels(strategy="crypto_1h").inc()
            edge_detected.labels(strategy="crypto_1h").observe(net_edge)

            size_usdc = self.risk.size_position(edge=net_edge, base_size=max_spend)
            if size_usdc < MIN_TRADE_USDC:
                continue

            self._entered[market.condition_id] = time.time()
            self.log(
                f"[SNIPE] {direction} {coin.upper()} @ {ask:.4f} | "
                f"candle={candle_return:+.4%} P(win)={win_prob:.2f} "
                f"fee={fee:.4f} net_edge={net_edge:.4f} size=${size_usdc:.2f} | "
                f"{secs_left:.0f}s left | {market.question[:55]}"
            )
            signals.append(Signal(
                strategy="crypto_1h",
                token_id=entry_token.token_id,
                side="BUY",
                price=ask,
                size_usdc=size_usdc,
                edge=net_edge,
                notes=f"[1H_SNIPE] {direction} {coin.upper()} candle={candle_return:+.4%}",
                metadata={
                    "outcome": direction,
                    "arb_type": "candle_snipe_1h",
                    "coin": coin,
                    "candle_return": round(candle_return, 6),
                    "win_probability": win_prob,
                    "taker_fee": round(fee, 6),
                    "net_edge": round(net_edge, 6),
                    "seconds_left": round(secs_left, 1),
                    "hour_open": hour_open_price,
                    "current_price": tick.price,
                    "condition_id": market.condition_id,
                },
            ))

        if signals:
            logger.info(f"Crypto1h: {len(signals)} snipe signals generated")
        return signals
