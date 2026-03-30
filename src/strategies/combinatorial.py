"""
Strategy 2: Combinatorial / Cross-Market Arbitrage.

Finds logically inconsistent pricing between related markets.

Examples:
  - Straddle arb: P(A) + P(B) + P(C) > 1 for mutually exclusive events
  - Dominance arb: P("BTC > 80k") > P("BTC > 75k") — impossible since 80k > 75k
  - Calendar arb: P(event by month end) < P(event by year end) — same event, longer window

The strategy:
  1. Cluster markets by topic keywords
  2. Within each cluster, check for logical inconsistencies
  3. Generate signals to exploit inconsistencies
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.utils.constants import FEE_RATE, SLIPPAGE_RATE
from src.utils.metrics import arb_opportunities, edge_detected

# Precompute round-trip fee floor for dominance / mutex checks (hot path)
_FEE_ROUND_TRIP = 2 * (FEE_RATE + SLIPPAGE_RATE)


# ------------------------------------------------------------------ #
#  Keyword clusters to group related markets                           #
# ------------------------------------------------------------------ #
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "btc_price": ["bitcoin", "btc", "100k", "80k", "75k", "70k", "60k", "50k"],
    "eth_price": ["ethereum", "eth", "5k", "4k", "3k", "2k"],
    "sol_price": ["solana", "sol"],
    "election_us": ["president", "election", "trump", "harris", "democrat", "republican"],
    "fed_rate": ["fed", "federal reserve", "interest rate", "rate cut", "bps"],
    "recession": ["recession", "gdp", "unemployment"],
    "crypto_etf": ["etf", "spot bitcoin", "spot eth"],
}

import json as _json
import time as _time

_research_topics_cache: dict[str, list[str]] = {}
_research_topics_cache_ts: float = 0.0
_RESEARCH_TOPICS_TTL: float = 120.0  # re-read file at most once every 2 minutes


def _load_research_topics() -> dict[str, list[str]]:
    """Load dynamically injected topics from the latest research agent run (cached)."""
    global _research_topics_cache, _research_topics_cache_ts
    now = _time.monotonic()
    if now - _research_topics_cache_ts < _RESEARCH_TOPICS_TTL:
        return _research_topics_cache
    try:
        with open("logs/research_signals.json") as f:
            data = _json.load(f)
        topics = data.get("active_topics", [])
        result = {"research_hot": [kw.lower() for kw in topics[:20]]} if topics else {}
    except (OSError, ValueError):
        result = {}
    _research_topics_cache = result
    _research_topics_cache_ts = now
    return result


# Price threshold regex (e.g., "above 60000", "> 60k")
PRICE_RE = re.compile(r"[>$]?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?\b")


def _extract_price_level(text: str) -> float | None:
    """Extract a price level from a market question."""
    text = text.lower()
    m = PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    val = float(raw)
    if m.group(2):
        val *= 1000
    return val


def _question_to_topic(question: str) -> str | None:
    q = question.lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return topic
    return None


def _assign_topic(q_lower: str, dynamic_topics: dict[str, list[str]]) -> str | None:
    """First topic with a keyword hit wins (same order as dynamic_topics iteration)."""
    for topic, kws in dynamic_topics.items():
        for kw in kws:
            if kw in q_lower:
                return topic
    return None


class CombinatorialStrategy(BaseStrategy):
    """
    Detect cross-market arbitrage by clustering markets on topic and
    checking for probability ordering violations.
    """

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        signals: list[Signal] = []

        # 1. Cluster markets by topic
        clusters: dict[str, list[Market]] = defaultdict(list)
        # Merge in research-injected topics for this cycle
        dynamic_topics = {**TOPIC_KEYWORDS, **_load_research_topics()}
        for m in markets:
            if not m.active or m.closed:
                continue
            q_lower = m.question.lower()
            topic = _assign_topic(q_lower, dynamic_topics)
            if topic:
                clusters[topic].append(m)

        # 2. Check each cluster
        for topic, cluster_markets in clusters.items():
            if len(cluster_markets) < 2:
                continue
            new_signals = self._check_cluster(topic, cluster_markets, orderbooks)
            signals.extend(new_signals)

        return signals

    def _check_cluster(
        self,
        topic: str,
        markets: list[Market],
        orderbooks: dict[str, Orderbook],
    ) -> list[Signal]:
        signals = []

        # Build a list of (market, yes_token_id, no_token_id, yes_mid, no_mid, price_level)
        market_info = []
        for m in markets:
            yes_tok = no_tok = None
            for t in m.tokens:
                ol = t.outcome.lower()
                if ol == "yes":
                    yes_tok = t
                elif ol == "no":
                    no_tok = t
            if not yes_tok and m.tokens:
                yes_tok = m.tokens[0]
            if not no_tok and len(m.tokens) > 1:
                no_tok = m.tokens[1]
            if not yes_tok or not no_tok:
                continue
            yes_book = orderbooks.get(yes_tok.token_id)
            no_book = orderbooks.get(no_tok.token_id)
            if not yes_book or not no_book:
                continue
            yes_mid = yes_book.mid
            if yes_mid is None:
                continue
            price_level = _extract_price_level(m.question)
            market_info.append({
                "market": m,
                "yes_token_id": yes_tok.token_id,
                "no_token_id": no_tok.token_id,
                "yes_mid": yes_mid,
                "yes_ask": yes_book.best_ask,
                "yes_bid": yes_book.best_bid,
                "no_ask": no_book.best_ask,
                "no_bid": no_book.best_bid,
                "price_level": price_level,
            })

        if len(market_info) < 2:
            return signals

        # --- Dominance arb: "X > HIGH" can't be more likely than "X > LOW" ---
        # Sort by price level ascending
        price_markets = [m for m in market_info if m["price_level"] is not None]
        price_markets.sort(key=lambda x: x["price_level"])

        for i in range(len(price_markets) - 1):
            low_m = price_markets[i]    # P(X > LOW)  — should be higher probability
            high_m = price_markets[i + 1]  # P(X > HIGH) — should be lower probability

            low_yes = low_m["yes_mid"]
            high_yes = high_m["yes_mid"]

            # Violation: P(above HIGH) > P(above LOW)
            if high_yes > low_yes + 0.01:
                edge = high_yes - low_yes - _FEE_ROUND_TRIP
                min_edge = self.config.strategies.combo_min_edge
                if edge >= min_edge:
                    arb_opportunities.labels(strategy="combinatorial").inc()
                    edge_detected.labels(strategy="combinatorial").observe(edge)

                    size_usdc = self.risk.size_position(edge=edge)
                    self.log(
                        f"DOMINANCE ARB [{topic}] | "
                        f"P(>{high_m['price_level']:,.0f})={high_yes:.3f} > "
                        f"P(>{low_m['price_level']:,.0f})={low_yes:.3f} | "
                        f"edge={edge:.3f} | size=${size_usdc:.2f}"
                    )
                    has_high_pos = high_m["yes_token_id"] in self.portfolio.positions
                    # Buy the underpriced "low" YES, sell the overpriced "high" YES
                    if low_m["yes_ask"]:
                        signals.append(Signal(
                            strategy="combinatorial",
                            token_id=low_m["yes_token_id"],
                            side="BUY",
                            price=low_m["yes_ask"],
                            size_usdc=size_usdc / 2,
                            edge=edge,
                            notes=f"Dominance: buy LOW P(>{low_m['price_level']:,.0f})",
                            metadata={"topic": topic, "price_level": low_m["price_level"]},
                        ))
                    if high_m["yes_bid"] and has_high_pos:
                        signals.append(Signal(
                            strategy="combinatorial",
                            token_id=high_m["yes_token_id"],
                            side="SELL",
                            price=high_m["yes_bid"],
                            size_usdc=size_usdc / 2,
                            edge=edge,
                            notes=f"Dominance: sell HIGH P(>{high_m['price_level']:,.0f})",
                            metadata={"topic": topic, "price_level": high_m["price_level"]},
                        ))

        # --- Mutually exclusive events: sum > 1 means sell both ---
        # Only check small groups (2-4 markets) with plausible sums (1.05-1.5)
        if "election" in topic and 2 <= len(market_info) <= 4:
            total = sum(m["yes_mid"] for m in market_info)
            if 1.05 <= total <= 1.5:  # sanity check: realistic mutex overpricing only
                signals.extend(self._check_mutually_exclusive(topic, market_info))

        return signals

    def _check_mutually_exclusive(
        self, topic: str, market_info: list[dict]
    ) -> list[Signal]:
        """
        If sum of YES midpoints across mutually exclusive outcomes > 1,
        sell the overpriced positions.
        """
        signals = []
        total_prob = sum(m["yes_mid"] for m in market_info)

        if total_prob > 1.0 + _FEE_ROUND_TRIP:
            excess = total_prob - 1.0
            edge = excess - len(market_info) * _FEE_ROUND_TRIP
            if edge < self.config.strategies.combo_min_edge:
                return signals

            arb_opportunities.labels(strategy="combinatorial").inc()
            edge_detected.labels(strategy="combinatorial").observe(edge)
            self.log(
                f"MUTEX ARB [{topic}] | sum_of_YES={total_prob:.3f} | edge={edge:.3f}"
            )

            # Sell the most overpriced outcomes (those with highest mid)
            sorted_by_price = sorted(market_info, key=lambda x: -x["yes_mid"])
            size_usdc = self.risk.size_position(edge=edge)

            for info in sorted_by_price[:2]:  # sell top 2 overpriced
                token_id = info["yes_token_id"]
                if token_id in self.portfolio.positions and info["yes_bid"]:
                    signals.append(Signal(
                        strategy="combinatorial",
                        token_id=token_id,
                        side="SELL",
                        price=info["yes_bid"],
                        size_usdc=size_usdc / 2,
                        edge=edge,
                        notes=f"Mutex arb: sell overpriced YES [{topic}]",
                        metadata={"topic": topic, "total_prob": total_prob},
                    ))

        return signals
