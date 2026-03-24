"""
Strategy 3: Latency Arbitrage Against Binance Prices.

Polymarket crypto prediction markets reflect the probability of price targets
being hit. When Binance spot prices move significantly, Polymarket prediction
markets lag in updating their probabilities.

For example:
  - Binance BTC dumps 3% in 60 seconds
  - "Will BTC be above $70k by end of month?" is still priced at 0.65
  - The fair value just dropped (e.g. to 0.58)
  - We sell YES (or buy NO) before Polymarket market makers catch up

Model: We use a simple sigmoid model mapping BTC price -> YES probability
for "above $X" markets. Real implementations would use more sophisticated
option-pricing-inspired models.
"""
from __future__ import annotations

import math
import time
from typing import Any

from src.exchange.binance import BinanceFeed
from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.utils.metrics import arb_opportunities, edge_detected, price_lag


# Mapping: Binance symbol -> Polymarket keywords that identify related markets
CRYPTO_MARKETS: dict[str, list[str]] = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth"],
    "SOLUSDT": ["solana", "sol"],
    "BNBUSDT": ["bnb"],
    "DOGEUSDT": ["dogecoin", "doge"],
    "XRPUSDT": ["xrp", "ripple"],
}


def _norm_cdf(x: float) -> float:
    """Exact standard normal CDF using math.erfc."""
    return math.erfc(-x / math.sqrt(2)) / 2


def _fair_value_above_target(
    spot: float,
    target: float,
    days_to_expiry: float,
    vol: float = 0.80,  # implied annual vol for crypto
) -> float:
    """
    Black-Scholes risk-neutral probability that price will be >= target at expiry.
    Uses exact normal CDF (math.erfc) instead of a sigmoid approximation.
    """
    if spot <= 0 or target <= 0 or days_to_expiry <= 0:
        return 0.5
    t = days_to_expiry / 365.0
    sigma_sqrt_t = vol * math.sqrt(t)
    if sigma_sqrt_t < 1e-8:
        return 1.0 if spot >= target else 0.0
    # Risk-neutral d2: P(S_T >= K) = N(d2)
    d2 = (math.log(spot / target) + (-0.5 * vol ** 2) * t) / sigma_sqrt_t
    return _norm_cdf(d2)


def _days_to_expiry(end_date_iso: str) -> float:
    """Parse ISO date (date-only or full datetime) and return days until expiry."""
    try:
        from datetime import datetime, timezone
        s = end_date_iso.strip()
        # Normalise: strip trailing Z, then ensure timezone-aware
        s = s.rstrip("Z")
        if "T" not in s:
            # Date-only string like "2026-03-31" — treat as UTC midnight
            s += "T00:00:00"
        end = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (end - now).total_seconds() / 86400
        return max(delta, 0.001)
    except Exception:
        return 30.0  # default


class LatencyArbStrategy(BaseStrategy):
    """
    Monitor Binance for large price moves and exploit the lag in
    Polymarket crypto prediction markets.
    """

    def __init__(self, config, portfolio, risk_manager, binance_feed: BinanceFeed) -> None:
        super().__init__(config, portfolio, risk_manager)
        self.binance = binance_feed
        self._position_opened_at: dict[str, float] = {}  # token_id -> timestamp

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        signals: list[Signal] = []

        for binance_symbol, poly_keywords in CRYPTO_MARKETS.items():
            tick = self.binance.get_price(binance_symbol)
            if not tick:
                continue
            if self.binance.is_stale(binance_symbol, max_age_seconds=3.0):
                self.log(f"Stale Binance price for {binance_symbol}, skipping", "debug")
                continue

            spot = tick.price

            # Find related Polymarket markets
            for market in markets:
                if not market.active or market.closed:
                    continue
                q = market.question.lower()
                if not any(kw in q for kw in poly_keywords):
                    continue

                # Extract target price level from question
                from src.strategies.combinatorial import _extract_price_level
                target = _extract_price_level(market.question)
                if not target:
                    continue

                # Compute fair value
                dte = _days_to_expiry(market.end_date_iso)
                fair = _fair_value_above_target(spot=spot, target=target, days_to_expiry=dte)

                # Get current market price
                yes_tok = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
                no_tok = next((t for t in market.tokens if t.outcome.lower() == "no"), None)
                if not yes_tok or not no_tok:
                    continue

                yes_book = orderbooks.get(yes_tok.token_id)
                if not yes_book or not yes_book.mid:
                    continue

                market_yes = yes_book.mid
                deviation = fair - market_yes
                abs_dev = abs(deviation)

                # Record lag metric
                price_lag.observe(abs_dev)

                threshold = self.config.strategies.latency_price_lag_threshold
                fee_cost = 2 * 0.002   # round trip

                if abs_dev < threshold + fee_cost:
                    continue

                arb_opportunities.labels(strategy="latency_arb").inc()
                edge_detected.labels(strategy="latency_arb").observe(abs_dev - fee_cost)

                size_usdc = self.risk.size_position(edge=abs_dev - fee_cost)

                if deviation > 0:
                    # Market underpriced YES — buy YES
                    if yes_book.best_ask:
                        self.log(
                            f"LAG BUY YES | {market.question[:50]} | "
                            f"{binance_symbol}=${spot:,.0f} target=${target:,.0f} | "
                            f"fair={fair:.3f} market={market_yes:.3f} lag={deviation:+.3f} | "
                            f"size=${size_usdc:.2f}"
                        )
                        signals.append(Signal(
                            strategy="latency_arb",
                            token_id=yes_tok.token_id,
                            side="BUY",
                            price=yes_book.best_ask,
                            size_usdc=size_usdc,
                            edge=abs_dev - fee_cost,
                            notes=f"Latency arb: fair={fair:.3f} vs market={market_yes:.3f}",
                            metadata={
                                "binance_symbol": binance_symbol,
                                "spot": spot,
                                "target": target,
                                "fair": fair,
                                "market_price": market_yes,
                                "opened_at": time.time(),
                            },
                        ))
                        self._position_opened_at[yes_tok.token_id] = time.time()

                else:
                    # Market overpriced YES — buy NO (or sell YES if held)
                    if no_tok:
                        no_book = orderbooks.get(no_tok.token_id)
                        if no_book and no_book.best_ask:
                            no_fair = 1.0 - fair
                            no_market = no_book.mid or 0.5
                            self.log(
                                f"LAG BUY NO | {market.question[:50]} | "
                                f"{binance_symbol}=${spot:,.0f} target=${target:,.0f} | "
                                f"fair_no={no_fair:.3f} market_no={no_market:.3f} lag={-deviation:+.3f} | "
                                f"size=${size_usdc:.2f}"
                            )
                            signals.append(Signal(
                                strategy="latency_arb",
                                token_id=no_tok.token_id,
                                side="BUY",
                                price=no_book.best_ask,
                                size_usdc=size_usdc,
                                edge=abs_dev - fee_cost,
                                notes=f"Latency arb NO: fair_no={no_fair:.3f} vs {no_market:.3f}",
                                metadata={
                                    "binance_symbol": binance_symbol,
                                    "spot": spot,
                                    "target": target,
                                    "fair": fair,
                                    "opened_at": time.time(),
                                },
                            ))
                            self._position_opened_at[no_tok.token_id] = time.time()

        # --- Exit stale latency arb positions ---
        max_hold = self.config.strategies.latency_max_hold_seconds
        for token_id, opened_at in list(self._position_opened_at.items()):
            if time.time() - opened_at > max_hold:
                pos = self.portfolio.positions.get(token_id)
                if pos:
                    # Find bid price to exit
                    book = orderbooks.get(token_id)
                    if book and book.best_bid:
                        self.log(
                            f"EXIT latency arb {token_id[:16]} (held {max_hold}s)"
                        )
                        signals.append(Signal(
                            strategy="latency_arb",
                            token_id=token_id,
                            side="SELL",
                            price=book.best_bid,
                            size_usdc=pos.cost_basis,
                            edge=0.0,
                            notes=f"Exit: max hold time {max_hold}s reached",
                        ))
                del self._position_opened_at[token_id]

        return signals
