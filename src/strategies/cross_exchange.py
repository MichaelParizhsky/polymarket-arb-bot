"""
Strategy: Cross-Exchange Arbitrage (Polymarket vs Kalshi).

Scans for the same event priced differently on Polymarket and Kalshi.
When the YES price on one exchange is meaningfully cheaper than the
YES bid on the other, after accounting for round-trip fees, we buy the
cheap side.

Paper-trading note
------------------
Kalshi does not support shorting in paper mode, so only the BUY leg is
executed when paper_trading=True.  The theoretical combined edge is still
logged so performance can be tracked.

Signal generation
-----------------
  Case A – buy on Polymarket, sell on Kalshi (live only):
    poly_yes_ask  <  kalshi_yes_bid  - 2 * FEE_RATE

  Case B – buy on Kalshi, sell on Polymarket (live only):
    kalshi_yes_ask  <  poly_yes_bid  - 2 * FEE_RATE

The minimum net edge threshold is read from
    config.strategies.combo_min_edge
(reusing the same knob as the combinatorial strategy).
"""
from __future__ import annotations

import re
from typing import Any

from src.exchange.kalshi import KalshiClient, KalshiMarket
from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.utils.constants import FEE_RATE
from src.utils.logger import logger
from src.utils.metrics import arb_opportunities, edge_detected

# Resolution criteria safety: keywords that indicate mechanical/unambiguous resolution
# (price at specific time). Safe to arb because both platforms resolve identically.
_MECHANICAL_KEYWORDS = {
    "above", "below", "over", "under", "exceed", "reach", "price", "at or above",
    "at or below", "higher than", "lower than", "close", "trading at",
}

# High-risk categories — resolution criteria often diverge between platforms
_RISKY_KEYWORDS = {
    "win", "election", "vote", "president", "senator", "governor", "candidate",
    "shutdown", "bill", "pass", "impeach", "resign", "acquit", "convict",
    "indict", "arrest", "war", "invasion", "ceasefire", "treaty", "deal",
    "default", "bankrupt", "merge", "acquire", "ipo",
}


# ------------------------------------------------------------------ #
#  Keyword sets used for cross-exchange market matching               #
# ------------------------------------------------------------------ #

# Each entry: (canonical_name, set_of_synonyms_lowercase)
_MATCH_GROUPS: list[tuple[str, set[str]]] = [
    # Crypto price levels — numeric tokens handle the actual numbers,
    # these ensure both sides are talking about the same asset.
    ("btc",        {"bitcoin", "btc"}),
    ("eth",        {"ethereum", "eth", "ether"}),
    ("sol",        {"solana", "sol"}),
    ("xrp",        {"xrp", "ripple"}),
    ("doge",       {"dogecoin", "doge"}),
    ("bnb",        {"bnb", "binance coin"}),

    # Election candidates / offices
    ("trump",      {"trump", "donald trump"}),
    ("harris",     {"harris", "kamala"}),
    ("biden",      {"biden"}),
    ("desantis",   {"desantis", "de santis"}),
    ("gop",        {"republican", "gop", "rnc"}),
    ("dem",        {"democrat", "democratic", "dnc"}),
    ("president",  {"president", "presidency", "presidential", "potus"}),
    ("senate",     {"senate", "senator"}),
    ("house",      {"house of representatives", "congress", "congressional"}),

    # Macro / Fed
    ("fed",        {"federal reserve", "fed", "fomc", "powell"}),
    ("rate_cut",   {"rate cut", "rate hike", "interest rate", "basis points", "bps"}),
    ("recession",  {"recession", "gdp", "unemployment"}),
    ("inflation",  {"inflation", "cpi", "pce"}),

    # Sports
    ("nba",        {"nba", "basketball"}),
    ("nfl",        {"nfl", "super bowl", "football"}),
    ("mlb",        {"mlb", "world series", "baseball"}),
    ("nhl",        {"nhl", "stanley cup", "hockey"}),
    ("fifa",       {"fifa", "world cup", "soccer", "football"}),

    # Other common prediction-market topics
    ("ai",         {"openai", "chatgpt", "gpt", "gemini", "anthropic", "claude"}),
    ("spacex",     {"spacex", "starship", "elon musk", "musk"}),
]

# Extract numeric price levels (e.g. "above $80,000", "over 80k", "> 80000")
_PRICE_LEVEL_RE = re.compile(r"[>$]?\s*(\d[\d,]*(?:\.\d+)?)\s*[kK]?\b")


def _extract_keywords(text: str) -> set[str]:
    """
    Return a set of canonical keyword tags that appear in *text*.
    Also includes any extracted price-level numbers (as strings like "80000")
    to help match markets that reference the same price target.
    """
    lower = text.lower()
    tags: set[str] = set()

    for canonical, synonyms in _MATCH_GROUPS:
        if any(syn in lower for syn in synonyms):
            tags.add(canonical)

    # Add normalised price levels so "BTC > 80k" and "KXBTCD-T80000" match.
    for m in _PRICE_LEVEL_RE.finditer(lower):
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
            # Normalise "80k" -> 80000
            suffix_start = m.end()
            if suffix_start < len(lower) and lower[suffix_start] == "k":
                val *= 1000
            tags.add(str(int(val)))
        except ValueError:
            pass

    return tags


# ------------------------------------------------------------------ #
#  Strategy                                                            #
# ------------------------------------------------------------------ #

class CrossExchangeStrategy(BaseStrategy):
    """
    Detect and exploit YES-price divergences between Polymarket and Kalshi
    for the same underlying event.
    """

    def __init__(
        self,
        config,
        portfolio,
        risk_manager,
        kalshi_client: KalshiClient,
    ) -> None:
        super().__init__(config, portfolio, risk_manager)
        self.kalshi = kalshi_client

    # ------------------------------------------------------------------ #
    #  Public scan entry-point                                             #
    # ------------------------------------------------------------------ #

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        poly_markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        kalshi_markets: list[KalshiMarket] = context.get("kalshi_markets", [])

        signals: list[Signal] = []

        if not kalshi_markets:
            # Nothing to compare against — not an error, Kalshi creds may be absent.
            logger.debug("[CROSS-EXCHANGE] No Kalshi markets available, skipping scan")
            return signals

        # Use cross_exchange_min_edge (default 5%) — must clear ~4-5% combined fees
        min_edge: float = getattr(
            self.config.strategies, "cross_exchange_min_edge",
            self.config.strategies.combo_min_edge
        )
        safe_only: bool = getattr(self.config.strategies, "cross_exchange_safe_only", True)
        fee_cost = 2 * FEE_RATE  # both legs

        for poly_market in poly_markets:
            if not poly_market.active or poly_market.closed:
                continue

            # Resolve YES token and its orderbook.
            yes_tok = next(
                (t for t in poly_market.tokens if t.outcome.lower() == "yes"), None
            )
            if not yes_tok:
                continue
            poly_book = orderbooks.get(yes_tok.token_id)
            if not poly_book:
                continue

            poly_bid = poly_book.best_bid
            poly_ask = poly_book.best_ask
            if poly_bid is None or poly_ask is None:
                continue

            # Find a matching Kalshi market.
            k_market = self._match_markets(poly_market, kalshi_markets)
            if k_market is None:
                continue

            # Safety check: skip markets with ambiguous/divergent resolution criteria
            if safe_only and not self._is_safe_market_pair(poly_market.question, k_market.title):
                logger.debug(
                    f"[CROSS-EXCHANGE] Skipping risky market pair: '{poly_market.question[:40]}'"
                )
                continue

            # Fetch Kalshi orderbook for live bid/ask, fall back to top-level prices.
            k_yes_bid, k_yes_ask = await self._kalshi_prices(k_market)
            if k_yes_bid is None or k_yes_ask is None:
                continue

            question_snippet = poly_market.question[:50]

            # ---------------------------------------------------------- #
            #  Case A: buy cheap YES on Polymarket, sell YES on Kalshi    #
            # ---------------------------------------------------------- #
            edge_a = k_yes_bid - poly_ask - fee_cost
            if edge_a >= min_edge:
                arb_opportunities.labels(strategy="cross_exchange").inc()
                edge_detected.labels(strategy="cross_exchange").observe(edge_a)
                size_usdc = self.risk.size_position(edge=edge_a)

                logger.info(
                    f"[CROSS-EXCHANGE] Poly YES@{poly_ask:.4f} vs Kalshi YES@{k_yes_bid:.4f}"
                    f" | edge={edge_a:.4f} | {question_snippet}"
                )

                # Always execute the buy leg.
                signals.append(Signal(
                    strategy="cross_exchange",
                    token_id=yes_tok.token_id,
                    side="BUY",
                    price=poly_ask,
                    size_usdc=size_usdc,
                    edge=edge_a,
                    notes=(
                        f"Cross-exchange A: buy Poly YES@{poly_ask:.4f}, "
                        f"sell Kalshi YES@{k_yes_bid:.4f}"
                    ),
                    metadata={
                        "kalshi_ticker": k_market.ticker,
                        "kalshi_bid": k_yes_bid,
                        "poly_ask": poly_ask,
                        "edge": edge_a,
                        "direction": "poly_buy",
                    },
                ))

                if self.kalshi.paper_trading:
                    logger.info(
                        f"[CROSS-EXCHANGE] [PAPER] Skipping Kalshi SELL leg "
                        f"(no shorting in paper mode) | {k_market.ticker}"
                    )
                else:
                    # Sell leg on Kalshi is handled by the execution layer using
                    # the metadata above; emit a companion signal for it.
                    signals.append(Signal(
                        strategy="cross_exchange",
                        token_id=k_market.ticker,   # use ticker as token_id for Kalshi
                        side="SELL",
                        price=k_yes_bid,
                        size_usdc=size_usdc,
                        edge=edge_a,
                        notes=(
                            f"Cross-exchange A: sell Kalshi YES@{k_yes_bid:.4f} "
                            f"(hedge for Poly buy)"
                        ),
                        metadata={
                            "kalshi_ticker": k_market.ticker,
                            "exchange": "kalshi",
                            "direction": "kalshi_sell",
                        },
                    ))

            # ---------------------------------------------------------- #
            #  Case B: buy cheap YES on Kalshi, sell YES on Polymarket    #
            # ---------------------------------------------------------- #
            edge_b = poly_bid - k_yes_ask - fee_cost
            if edge_b >= min_edge:
                arb_opportunities.labels(strategy="cross_exchange").inc()
                edge_detected.labels(strategy="cross_exchange").observe(edge_b)
                size_usdc = self.risk.size_position(edge=edge_b)

                logger.info(
                    f"[CROSS-EXCHANGE] Kalshi YES@{k_yes_ask:.4f} vs Poly YES@{poly_bid:.4f}"
                    f" | edge={edge_b:.4f} | {question_snippet}"
                )

                # Kalshi buy leg (always).
                signals.append(Signal(
                    strategy="cross_exchange",
                    token_id=k_market.ticker,
                    side="BUY",
                    price=k_yes_ask,
                    size_usdc=size_usdc,
                    edge=edge_b,
                    notes=(
                        f"Cross-exchange B: buy Kalshi YES@{k_yes_ask:.4f}, "
                        f"sell Poly YES@{poly_bid:.4f}"
                    ),
                    metadata={
                        "kalshi_ticker": k_market.ticker,
                        "exchange": "kalshi",
                        "kalshi_ask": k_yes_ask,
                        "poly_bid": poly_bid,
                        "edge": edge_b,
                        "direction": "kalshi_buy",
                    },
                ))

                if self.kalshi.paper_trading:
                    logger.info(
                        f"[CROSS-EXCHANGE] [PAPER] Skipping Poly SELL leg "
                        f"(paper mode, no short) | {poly_market.condition_id}"
                    )
                else:
                    signals.append(Signal(
                        strategy="cross_exchange",
                        token_id=yes_tok.token_id,
                        side="SELL",
                        price=poly_bid,
                        size_usdc=size_usdc,
                        edge=edge_b,
                        notes=(
                            f"Cross-exchange B: sell Poly YES@{poly_bid:.4f} "
                            f"(hedge for Kalshi buy)"
                        ),
                        metadata={
                            "kalshi_ticker": k_market.ticker,
                            "poly_bid": poly_bid,
                            "direction": "poly_sell",
                        },
                    ))

        return signals

    # ------------------------------------------------------------------ #
    #  Resolution safety check                                             #
    # ------------------------------------------------------------------ #

    def _is_safe_market_pair(self, poly_question: str, kalshi_title: str) -> bool:
        """
        Return True only if both market questions appear to use mechanical,
        unambiguous resolution criteria (e.g., price above $X at time Y).

        Markets with political/event outcome resolution are excluded because
        Polymarket and Kalshi frequently diverge on resolution criteria for
        semantically similar but legally-distinct questions, causing total
        loss on one leg (documented in 2024 election and shutdown markets).

        Only BTC/ETH/SOL/XRP/SOL price markets at a specific time are
        considered inherently safe for cross-exchange arbitrage.
        """
        combined = (poly_question + " " + kalshi_title).lower()

        # Block if any risky keyword appears
        if any(kw in combined for kw in _RISKY_KEYWORDS):
            return False

        # Require at least one mechanical keyword (price-level language)
        has_mechanical = any(kw in combined for kw in _MECHANICAL_KEYWORDS)

        # Require a crypto asset reference for maximum safety
        has_crypto = any(c in combined for c in ("btc", "bitcoin", "eth", "ethereum", "sol", "xrp"))

        return has_mechanical and has_crypto

    # ------------------------------------------------------------------ #
    #  Market matching                                                     #
    # ------------------------------------------------------------------ #

    def _match_markets(
        self,
        poly_market: Market,
        kalshi_markets: list[KalshiMarket],
    ) -> KalshiMarket | None:
        """
        Return the best-matching KalshiMarket for *poly_market*, or None.

        Matching is keyword-overlap based: we extract canonical tags from
        the Polymarket question and each Kalshi title, then rank by the
        size of the intersection.  A minimum overlap of 2 tags is required
        to avoid false positives (e.g., two completely different markets
        that both mention "BTC").

        The Kalshi ticker itself is also scanned for price level numbers
        (e.g., "KXBTCD-T100000" contains "100000") so pure numeric price-
        level markets match correctly even when the title wording differs.
        """
        poly_tags = _extract_keywords(poly_market.question)
        if not poly_tags:
            return None

        best_match: KalshiMarket | None = None
        best_score: int = 0

        for km in kalshi_markets:
            # Combine title keywords + price levels embedded in the ticker.
            kalshi_tags = _extract_keywords(km.title) | _extract_keywords(km.ticker)
            overlap = len(poly_tags & kalshi_tags)
            if overlap > best_score and overlap >= 2:
                best_score = overlap
                best_match = km

        if best_match:
            logger.debug(
                f"[CROSS-EXCHANGE] Matched '{poly_market.question[:40]}' "
                f"-> Kalshi '{best_match.ticker}' (score={best_score})"
            )

        return best_match

    # ------------------------------------------------------------------ #
    #  Kalshi price resolution                                             #
    # ------------------------------------------------------------------ #

    async def _kalshi_prices(
        self, km: KalshiMarket
    ) -> tuple[float | None, float | None]:
        """
        Return (yes_bid, yes_ask) for a Kalshi market.

        Attempts to fetch the live orderbook first; if that fails or the
        client has no credentials, falls back to the top-of-book prices
        stored in the KalshiMarket dataclass (populated when markets were
        fetched).  Returns (None, None) if no price is available at all.
        """
        try:
            book = await self.kalshi.get_orderbook(km.ticker)
            if book is not None:
                bid = book.best_yes_bid
                ask = book.best_yes_ask
                if bid is not None and ask is not None:
                    return bid, ask
        except Exception as exc:
            logger.debug(
                f"[CROSS-EXCHANGE] Orderbook fetch failed for {km.ticker}: {exc}; "
                "falling back to market-level prices"
            )

        # Fall back to snapshot prices from the market list.
        bid = km.yes_bid if km.yes_bid > 0 else None
        ask = km.yes_ask if km.yes_ask > 0 else None
        return bid, ask
