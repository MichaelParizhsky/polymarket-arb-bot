"""
strategies/latency_arb.py — Latency Arbitrage

Polymarket's crypto price prediction markets (e.g. "Will BTC be above $70k by EOD?")
lag behind real-time Binance prices. When a confirmed price move hasn't yet been
reflected in Polymarket's odds, there's a tradeable edge.

Strategy:
  1. Track Binance spot price for BTC/ETH/SOL in real time
  2. Find Polymarket markets whose implied probabilities are inconsistent
     with the current spot price
  3. Trade the mispriced direction with confidence weighting
"""
from __future__ import annotations
import os
import re
from typing import List, Optional, Dict

from loguru import logger

from src.models import Market, LatencyArbOpportunity, TradeDirection
from src.data.price_feed import BinancePriceFeed


LAG_THRESHOLD = float(os.getenv("LATENCY_ARB_PRICE_LAG_THRESHOLD", "0.008"))
MAX_POSITION  = float(os.getenv("LATENCY_ARB_MAX_POSITION_USDC",   "200"))
SYMBOLS       = [s.strip().upper() for s in os.getenv("LATENCY_ARB_SYMBOLS", "BTC,ETH,SOL").split(",")]


class LatencyArbStrategy:
    """
    Detects when Polymarket's implied probability for a crypto price market
    is stale relative to the actual Binance spot price.

    Example:
      - Binance: BTC = $72,500 (confirmed)
      - Polymarket: "BTC above $70k by EOD?" still priced at 0.62 (YES)
      - Correct probability given spot = ~0.85 → lag = 0.23 → buy YES
    """

    def __init__(self, price_feed: BinancePriceFeed):
        self.feed = price_feed
        self.opportunities_found = 0

    def scan(self, markets: List[Market]) -> List[LatencyArbOpportunity]:
        opps: List[LatencyArbOpportunity] = []

        crypto_markets = [m for m in markets if self._is_crypto_market(m)]
        logger.debug(f"[LatencyArb] Scanning {len(crypto_markets)} crypto markets")

        for market in crypto_markets:
            opp = self._evaluate(market)
            if opp:
                opps.append(opp)
                self.opportunities_found += 1

        if opps:
            logger.info(f"[LatencyArb] Found {len(opps)} latency opportunities")
        return sorted(opps, key=lambda o: abs(o.lag_pct), reverse=True)

    def _evaluate(self, market: Market) -> Optional[LatencyArbOpportunity]:
        symbol = self._extract_symbol(market.question)
        if not symbol:
            return None

        spot = self.feed.get_price(symbol)
        if not spot:
            return None

        # Extract price threshold from question (e.g. "$70,000" or "70k")
        threshold = self._extract_price_threshold(market.question)
        if not threshold:
            return None

        # Estimate correct probability given spot vs threshold
        # Simple sigmoid-like model: further from threshold = higher confidence
        distance_pct = (spot - threshold) / threshold
        # Normalize: ±10% range → 0..1 probability
        implied_correct = self._distance_to_probability(distance_pct)

        # Get Polymarket's current YES price
        yes_outcome = next(
            (o for o in market.outcomes if "yes" in o.name.lower()), None
        )
        if not yes_outcome:
            return None

        poly_price = yes_outcome.price
        lag_pct    = abs(implied_correct - poly_price)

        if lag_pct < LAG_THRESHOLD:
            return None

        # Direction: if correct prob > poly price, BUY YES; else BUY NO
        direction = TradeDirection.BUY if implied_correct > poly_price else TradeDirection.SELL
        confidence = min(lag_pct / 0.15, 1.0)   # cap at 100%

        logger.debug(
            f"[LatencyArb] {symbol} spot=${spot:,.0f} vs threshold=${threshold:,.0f} | "
            f"poly={poly_price:.3f} implied={implied_correct:.3f} lag={lag_pct:.3f}"
        )

        return LatencyArbOpportunity(
            market        = market,
            symbol        = symbol,
            exchange_price= spot,
            poly_implied  = poly_price,
            lag_pct       = lag_pct,
            direction     = direction,
            confidence    = confidence,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_crypto_market(self, market: Market) -> bool:
        q = market.question.lower()
        return any(sym.lower() in q for sym in SYMBOLS) and len(market.outcomes) == 2

    def _extract_symbol(self, question: str) -> Optional[str]:
        q = question.upper()
        for sym in SYMBOLS:
            if sym in q:
                return sym
        return None

    def _extract_price_threshold(self, question: str) -> Optional[float]:
        """Pull dollar amount from question text like 'above $72,000' or 'over 70k'."""
        # Match patterns like $70,000 or $70k or 70,000
        patterns = [
            r"\$([0-9,]+(?:\.[0-9]+)?)[kK]?",
            r"([0-9,]+(?:\.[0-9]+)?)[kK]",
        ]
        for p in patterns:
            m = re.search(p, question.replace(",", ""))
            if m:
                val = float(m.group(1).replace(",", ""))
                # Handle "k" multiplier
                if question[m.end()-1].lower() == "k" or \
                   (m.end() < len(question) and question[m.end()].lower() == "k"):
                    val *= 1000
                return val
        return None

    def _distance_to_probability(self, distance_pct: float) -> float:
        """
        Convert normalized distance (spot vs threshold) to a probability estimate.
        distance_pct > 0 means spot ABOVE threshold (YES more likely).
        Uses a simple logistic-like mapping: ±10% → ~90% / ~10%.
        """
        import math
        k = 20.0   # steepness
        return 1.0 / (1.0 + math.exp(-k * distance_pct))
