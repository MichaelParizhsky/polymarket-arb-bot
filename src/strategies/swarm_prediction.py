"""
Strategy: Swarm Prediction — Crowd Simulation Mispricing.

Inspired by MiroFish (github.com/666ghj/MiroFish). Instead of predicting
outcomes directly, simulates how a crowd of humans with diverse personalities
will price a prediction market — then trades when the crowd's consensus
diverges from current market price.

Two modes:
1. MiroFish mode (if MIROFISH_URL is set): calls the full MiroFish Node.js
   REST API for a proper multi-thousand-agent simulation.
2. LLM persona mode (fallback): uses Perplexity sonar-reasoning-pro to
   simulate SWARM_AGENT_COUNT diverse personas and aggregate their estimates.

Edge: The crowd systematically misprices markets when:
  - Availability bias: over-weights recent vivid events (e.g., last game's score)
  - Narrative bias: under-reacts to base rates in favor of compelling stories
  - Anchoring: price anchors at round numbers (0.50, 0.75) longer than data warrants

Signals fire when |simulated_prob - market_price| > swarm_min_edge AND
confidence > swarm_min_confidence.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.utils.ai_research import perplexity
from src.utils.constants import MIN_TRADE_USDC, calc_taker_fee
from src.utils.metrics import arb_opportunities, edge_detected

# ---------------------------------------------------------------------------
# Persona list used for LLM simulation when MiroFish is not available.
# These 12 archetypes span the key cognitive biases that cause crowd mispricing.
# ---------------------------------------------------------------------------
_PERSONAS: list[str] = [
    "casual sports fan who just watched the highlights",
    "professional gambler focused only on line movement",
    "statistician who reads only box scores",
    "social media user influenced by Twitter hype",
    "contrarian investor who fades the public",
    "domain expert with deep knowledge of this specific market category",
    "momentum trader who extrapolates recent trends",
    "value investor anchored to historical base rates",
    "news reader who just saw a breaking headline",
    "emotional bettor with recency bias",
    "systematic quant who uses only probability models",
    "risk-averse retail bettor who avoids extreme positions",
]

# JSON schema for structured Perplexity response
_SWARM_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "swarm_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "persona_estimates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "persona": {"type": "string"},
                            "estimate": {"type": "number"},
                        },
                        "required": ["persona", "estimate"],
                        "additionalProperties": False,
                    },
                },
                "aggregate_probability": {"type": "number"},
                "confidence": {"type": "number"},
                "crowd_bias": {"type": "string"},
                "bias_reason": {"type": "string"},
            },
            "required": [
                "persona_estimates",
                "aggregate_probability",
                "confidence",
                "crowd_bias",
                "bias_reason",
            ],
            "additionalProperties": False,
        },
    },
}


class SwarmPredictionStrategy(BaseStrategy):
    """
    Simulates crowd behavior via LLM persona aggregation or a MiroFish
    multi-agent REST service, then trades when the simulated consensus
    diverges from the live market price by more than swarm_min_edge.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        # condition_id -> entered_at (unix timestamp)
        self._entered: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        if not perplexity.enabled:
            self.log(
                "Perplexity API key not set — skipping SwarmPrediction scan", "warning"
            )
            return []

        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})

        cfg = self.config.strategies
        min_volume: float = getattr(cfg, "swarm_min_volume", 500.0)
        min_edge: float = getattr(cfg, "swarm_min_edge", 0.05)
        min_confidence: float = getattr(cfg, "swarm_min_confidence", 0.55)
        max_spend: float = getattr(cfg, "swarm_max_spend", 50.0)
        max_markets: int = getattr(cfg, "swarm_max_markets_per_cycle", 5)
        cooldown_hours: float = getattr(cfg, "swarm_cooldown_hours", 6.0)
        agent_count: int = getattr(cfg, "swarm_agent_count", len(_PERSONAS))
        mirofish_url: str = os.getenv("MIROFISH_URL", "").rstrip("/")

        self._prune_cooldown(cooldown_hours)

        # Filter: active, sufficient volume, mid in [0.15, 0.85]
        candidates: list[tuple[float, Market, Orderbook, str, str]] = []
        for market in markets:
            if not market.active or market.closed:
                continue
            volume = market.volume or 0.0
            if volume < min_volume:
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
            if not yes_book:
                continue

            mid = yes_book.mid
            if mid is None:
                mid = yes_book.best_ask or yes_book.best_bid
            if mid is None:
                continue

            # Avoid near-resolved markets — no crowd mispricing to exploit
            if mid < 0.15 or mid > 0.85:
                continue

            # Skip if in cooldown
            if market.condition_id in self._entered:
                continue

            candidates.append((volume, market, yes_book, yes_token.token_id, no_token.token_id))

        # Sort by volume descending, take top N
        candidates.sort(key=lambda x: x[0], reverse=True)
        candidates = candidates[: max_markets]

        signals: list[Signal] = []
        for _, market, yes_book, yes_token_id, no_token_id in candidates:
            try:
                sig = await self._evaluate_market(
                    market=market,
                    yes_book=yes_book,
                    yes_token_id=yes_token_id,
                    no_token_id=no_token_id,
                    orderbooks=orderbooks,
                    min_edge=min_edge,
                    min_confidence=min_confidence,
                    max_spend=max_spend,
                    agent_count=agent_count,
                    mirofish_url=mirofish_url,
                )
                if sig is not None:
                    signals.append(sig)
            except Exception as exc:
                self.log(
                    f"Error evaluating {market.question[:60]}: {exc}", "warning"
                )

        self.log(
            f"scan: {len(markets)} markets → {len(candidates)} candidates → "
            f"{len(signals)} signals"
        )
        return signals

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune_cooldown(self, cooldown_hours: float) -> None:
        cutoff = time.time() - cooldown_hours * 3600.0
        expired = [cid for cid, ts in self._entered.items() if ts < cutoff]
        for cid in expired:
            del self._entered[cid]

    async def _evaluate_market(
        self,
        market: Market,
        yes_book: Orderbook,
        yes_token_id: str,
        no_token_id: str,
        orderbooks: dict[str, Orderbook],
        min_edge: float,
        min_confidence: float,
        max_spend: float,
        agent_count: int,
        mirofish_url: str,
    ) -> Signal | None:
        mid = yes_book.mid
        if mid is None:
            mid = yes_book.best_ask or yes_book.best_bid
        if mid is None:
            return None

        # Fetch rich multi-source research via Agent API (search + fetch_url loop).
        # Falls back to single Sonar call automatically if Agent API unavailable.
        news = await perplexity.agent(
            f"Research the current status and any recent developments for this prediction market question: "
            f'"{market.question}". '
            f"Find: (1) the most recent relevant news or data, "
            f"(2) the current expert or public consensus probability, "
            f"(3) any upcoming events that could move the outcome. "
            f"Be concise — 3-5 sentences max."
        )
        news_snippet = news[:800] if news else "No recent news found."

        # Run crowd simulation
        if mirofish_url:
            prob, confidence = await self._call_mirofish(
                market=market,
                news=news_snippet,
                mirofish_url=mirofish_url,
                agent_count=agent_count,
            )
            crowd_bias = "overpriced" if prob < mid else "underpriced"
            bias_reason = f"MiroFish simulation with {agent_count} agents"
            simulation_mode = "mirofish"
        else:
            result = await self._simulate_crowd(
                market=market,
                news_snippet=news_snippet,
                current_mid=mid,
                agent_count=agent_count,
            )
            if not result:
                return None
            prob = result.get("aggregate_probability", mid)
            confidence = result.get("confidence", 0.0)
            crowd_bias = result.get("crowd_bias", "fair")
            bias_reason = result.get("bias_reason", "")
            simulation_mode = "llm_persona"

        if confidence < 0.3:
            self.log(
                f"[SWARM] low confidence {confidence:.2f} for {market.question[:50]} — skip",
                "debug",
            )
            return None

        fee = calc_taker_fee(mid, "standard")
        gross_edge = abs(prob - mid)
        net_edge = gross_edge - fee

        if net_edge < min_edge:
            self.log(
                f"[SWARM] net_edge={net_edge:.4f} < {min_edge:.4f} for {market.question[:50]} — skip",
                "debug",
            )
            return None

        if confidence < min_confidence:
            self.log(
                f"[SWARM] confidence={confidence:.2f} < {min_confidence:.2f} for {market.question[:50]} — skip",
                "debug",
            )
            return None

        # Choose which token to buy
        if prob > mid:
            # Crowd underprices YES
            direction = "BUY YES"
            token_id = yes_token_id
            best_ask = yes_book.best_ask
            if best_ask is None:
                return None
        else:
            # Crowd underprices NO
            direction = "BUY NO"
            token_id = no_token_id
            no_book = orderbooks.get(no_token_id)
            if no_book is None or no_book.best_ask is None:
                return None
            best_ask = no_book.best_ask

        size_usdc = self.risk.size_position(edge=net_edge, base_size=max_spend)
        if size_usdc < MIN_TRADE_USDC:
            return None

        arb_opportunities.labels(strategy="swarm_prediction").inc()
        edge_detected.labels(strategy="swarm_prediction").observe(net_edge)

        self._entered[market.condition_id] = time.time()

        notes = (
            f"[SWARM] {direction} | crowd_prob={prob:.3f} market={mid:.3f} "
            f"conf={confidence:.2f} | {bias_reason}"
        )
        self.log(
            f"{notes} | edge={net_edge:.4f} size=${size_usdc:.2f} | {market.question[:60]}"
        )

        return Signal(
            strategy="swarm_prediction",
            token_id=token_id,
            side="BUY",
            price=best_ask,
            size_usdc=size_usdc,
            edge=net_edge,
            notes=notes,
            metadata={
                "crowd_probability": prob,
                "market_price": mid,
                "confidence": confidence,
                "crowd_bias": crowd_bias,
                "bias_reason": bias_reason,
                "simulation_mode": simulation_mode,
                "condition_id": market.condition_id,
            },
        )

    async def _simulate_crowd(
        self,
        market: Market,
        news_snippet: str,
        current_mid: float,
        agent_count: int,
    ) -> dict | None:
        """
        Call Perplexity sonar-reasoning-pro with a structured prompt that asks
        it to simulate N diverse personas and return aggregate probability +
        confidence as JSON.
        """
        persona_list = _PERSONAS[:agent_count]
        personas_str = "\n".join(f"- {p}" for p in persona_list)

        prompt = (
            f'You are simulating {len(persona_list)} diverse crowd participants predicting: '
            f'"{market.question}"\n\n'
            f"Current market price: {current_mid:.3f} (this is the crowd's current consensus)\n"
            f"Recent news context: {news_snippet}\n\n"
            f"Personas to simulate:\n{personas_str}\n\n"
            "For EACH persona, estimate: what probability would they assign to YES?\n"
            "Consider their biases, information sources, and reasoning patterns.\n\n"
            "Then aggregate: what is the CROWD's true underlying probability,\n"
            "accounting for the biases each persona introduces?\n\n"
            "Return JSON:\n"
            "{\n"
            '  "persona_estimates": [{"persona": "...", "estimate": 0.xx}, ...],\n'
            '  "aggregate_probability": 0.xx,\n'
            '  "confidence": 0.xx,\n'
            '  "crowd_bias": "overpriced|underpriced|fair",\n'
            '  "bias_reason": "one sentence explanation of why crowd is biased"\n'
            "}"
        )

        result = await perplexity.search_structured(
            query=prompt,
            schema=_SWARM_SCHEMA,
            model="sonar-reasoning-pro",
        )

        if not result:
            self.log(
                f"Perplexity returned empty result for {market.question[:50]}", "warning"
            )
            return None

        # Validate required keys and numeric ranges
        prob = result.get("aggregate_probability")
        conf = result.get("confidence")
        if prob is None or conf is None:
            self.log(
                f"Swarm result missing keys for {market.question[:50]}: {result}", "warning"
            )
            return None
        if not (0.0 <= float(prob) <= 1.0) or not (0.0 <= float(conf) <= 1.0):
            self.log(
                f"Swarm result out of range prob={prob} conf={conf}", "warning"
            )
            return None

        self.log(
            f"[SWARM] persona sim: prob={prob:.3f} conf={conf:.2f} "
            f"bias={result.get('crowd_bias')} | {market.question[:50]}",
            "debug",
        )
        return result

    async def _call_mirofish(
        self,
        market: Market,
        news: str,
        mirofish_url: str,
        agent_count: int,
    ) -> tuple[float, float]:
        """
        POST to MiroFish Node.js REST API for a multi-agent simulation.
        Returns (probability, confidence). Falls back to (0.5, 0.0) on any error.
        """
        payload = {
            "seed": f"{market.question}\n\nContext: {news}",
            "agents": agent_count,
            "returns": {"probability": True, "confidence": True},
        }
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                r = await client.post(
                    f"{mirofish_url}/api/simulate",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                r.raise_for_status()
                data: dict = r.json()
                prob = float(data.get("probability", 0.5))
                conf = float(data.get("confidence", 0.0))
                prob = max(0.0, min(1.0, prob))
                conf = max(0.0, min(1.0, conf))
                self.log(
                    f"[SWARM] MiroFish: prob={prob:.3f} conf={conf:.2f} | {market.question[:50]}",
                    "debug",
                )
                return prob, conf
        except Exception as exc:
            self.log(f"MiroFish API error for {market.question[:50]}: {exc}", "warning")
            return 0.5, 0.0
