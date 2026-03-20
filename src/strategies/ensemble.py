"""
EnsembleStrategy — LLM-consensus prediction market signal generator.

Uses Claude Haiku + OpenAI GPT-4o-mini in parallel to estimate market
probabilities, then generates a trade signal when:
  1. Both models agree within 10% of each other (or single-model mode).
  2. Their consensus deviates from the market mid by >= ensemble_min_edge
     (after subtracting the taker fee).

Filters: volume >= $1000, resolving within 30 days, both YES/NO tokens present.
Cache: 15-minute TTL per condition_id.
Rate-limit: at most ensemble_max_markets_per_cycle markets per scan cycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

try:
    import anthropic as _anthropic_mod

    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    import openai as _openai_mod

    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

from src.strategies.base import BaseStrategy, Signal
from src.utils.constants import MIN_TRADE_USDC, calc_taker_fee

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS: float = 15 * 60  # 15 minutes
_MAX_VOLUME_DAYS: int = 30            # only markets resolving within 30 days
_MIN_VOLUME_USDC: float = 1_000.0     # minimum market volume
_AGREEMENT_THRESHOLD: float = 0.10   # max allowed difference between models
_SINGLE_MODEL_EDGE_MULTIPLIER: float = 1.5  # higher bar for single-model mode
_MAX_TOKENS: int = 64                 # tiny — we only want JSON back

_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
_OPENAI_MODEL = "gpt-4o-mini"

_PROMPT_TEMPLATE = """\
You are a prediction market probability estimator. Analyze this market and estimate the probability it resolves YES.

Market: {question}
Current market mid-price: {mid:.3f} (this represents the market's current probability estimate)
Days until resolution: {days:.1f}

Respond with ONLY valid JSON, no other text:
{{"probability": 0.XX, "confidence": "high|medium|low"}}

Be honest and calibrated. Your estimate should reflect the true probability, not just the current market price.\
"""

# ---------------------------------------------------------------------------
# Cache entry type alias: (stored_at, claude_prob, openai_prob)
# openai_prob is None when OpenAI is unavailable or failed.
# ---------------------------------------------------------------------------
_CacheEntry = tuple[float, float | None, float | None]


class EnsembleStrategy(BaseStrategy):
    """LLM ensemble strategy using Claude Haiku and GPT-4o-mini."""

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)

        # API clients (None if unavailable/unconfigured)
        self._anthropic_client: Any | None = None
        self._openai_client: Any | None = None
        self._init_clients()

        # condition_id -> (timestamp, claude_prob_or_None, openai_prob_or_None)
        self._cache: dict[str, _CacheEntry] = {}

    # ------------------------------------------------------------------
    # Client initialisation
    # ------------------------------------------------------------------

    def _init_clients(self) -> None:
        """Create SDK clients. Logs warnings on missing keys/packages."""
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        openai_key = os.environ.get("OPENAI_API_KEY", "")

        if _ANTHROPIC_AVAILABLE and anthropic_key:
            try:
                self._anthropic_client = _anthropic_mod.AsyncAnthropic(
                    api_key=anthropic_key
                )
                self.log(f"Anthropic client ready (model={_CLAUDE_MODEL})")
            except Exception as exc:
                self.log(f"Failed to init Anthropic client: {exc}", "warning")
        else:
            if not _ANTHROPIC_AVAILABLE:
                self.log("anthropic package not installed — Claude disabled", "warning")
            else:
                self.log("ANTHROPIC_API_KEY not set — Claude disabled", "warning")

        if _OPENAI_AVAILABLE and openai_key:
            try:
                self._openai_client = _openai_mod.AsyncOpenAI(api_key=openai_key)
                self.log(f"OpenAI client ready (model={_OPENAI_MODEL})")
            except Exception as exc:
                self.log(f"Failed to init OpenAI client: {exc}", "warning")
        else:
            if not _OPENAI_AVAILABLE:
                self.log("openai package not installed — GPT-4o-mini disabled", "warning")
            elif not openai_key:
                self.log("OPENAI_API_KEY not set — running single-model mode", "info")

    # ------------------------------------------------------------------
    # Prompt & response helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, question: str, mid: float, days: float) -> str:
        return _PROMPT_TEMPLATE.format(question=question, mid=mid, days=days)

    def _parse_probability(self, raw: str) -> float | None:
        """Extract probability float from raw JSON string. Returns None on failure."""
        try:
            # Strip any accidental markdown code fences
            text = raw.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(
                    l for l in lines if not l.startswith("```")
                ).strip()
            data = json.loads(text)
            prob = float(data["probability"])
            if not 0.0 <= prob <= 1.0:
                self.log(f"Probability out of range: {prob}", "warning")
                return None
            return prob
        except Exception as exc:
            self.log(f"Probability parse error ({exc}) — raw: {raw[:120]!r}", "warning")
            return None

    # ------------------------------------------------------------------
    # LLM call wrappers
    # ------------------------------------------------------------------

    async def _query_claude(self, prompt: str) -> float | None:
        """Call Claude Haiku; return probability or None."""
        if self._anthropic_client is None:
            return None
        try:
            response = await self._anthropic_client.messages.create(
                model=_CLAUDE_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text if response.content else ""
            return self._parse_probability(raw)
        except Exception as exc:
            self.log(f"Claude query failed: {exc}", "warning")
            return None

    async def _query_openai(self, prompt: str) -> float | None:
        """Call GPT-4o-mini; return probability or None."""
        if self._openai_client is None:
            return None
        try:
            response = await self._openai_client.chat.completions.create(
                model=_OPENAI_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.choices[0].message.content or ""
            return self._parse_probability(raw)
        except Exception as exc:
            self.log(f"OpenAI query failed: {exc}", "warning")
            return None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_get(self, condition_id: str) -> _CacheEntry | None:
        entry = self._cache.get(condition_id)
        if entry is None:
            return None
        stored_at = entry[0]
        if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
            del self._cache[condition_id]
            return None
        return entry

    def _cache_set(
        self,
        condition_id: str,
        claude_prob: float | None,
        openai_prob: float | None,
    ) -> None:
        self._cache[condition_id] = (time.monotonic(), claude_prob, openai_prob)

    # ------------------------------------------------------------------
    # Market filtering helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _days_to_resolution(end_date_iso: str) -> float | None:
        """Return days until resolution, or None if unparseable."""
        try:
            end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max(0.0, (end - now).total_seconds() / 86_400)
        except Exception:
            return None

    @staticmethod
    def _find_token(market: Any, outcome: str) -> str | None:
        """Return token_id for given outcome label, or None."""
        for tok in market.tokens:
            if tok.outcome.lower() == outcome.lower():
                return tok.token_id
        return None

    # ------------------------------------------------------------------
    # Per-market signal evaluation
    # ------------------------------------------------------------------

    async def _evaluate_market(
        self,
        market: Any,
        orderbooks: dict[str, Any],
        days: float,
        cached: _CacheEntry | None,
    ) -> Signal | None:
        """Query LLMs (or use cache), check edge, return Signal or None."""
        yes_token_id = self._find_token(market, "Yes")
        no_token_id = self._find_token(market, "No")
        if yes_token_id is None or no_token_id is None:
            return None

        ob_yes = orderbooks.get(yes_token_id)
        if ob_yes is None:
            return None

        mid = ob_yes.mid
        best_bid = ob_yes.best_bid
        best_ask = ob_yes.best_ask
        if mid is None or best_bid is None or best_ask is None:
            return None

        # ---- LLM queries (parallel, or use cache) ----
        if cached is not None:
            _, claude_prob, openai_prob = cached
        else:
            prompt = self._build_prompt(market.question, mid, days)
            claude_prob, openai_prob = await asyncio.gather(
                self._query_claude(prompt),
                self._query_openai(prompt),
            )
            self._cache_set(market.condition_id, claude_prob, openai_prob)

        # ---- Determine available models ----
        both_available = claude_prob is not None and openai_prob is not None
        one_available = (claude_prob is not None) or (openai_prob is not None)

        if not one_available:
            self.log(
                f"No valid probability from either model for: {market.question[:60]}",
                "warning",
            )
            return None

        # ---- Consensus & agreement check ----
        min_edge = getattr(self.config, "ensemble_min_edge", 0.05)

        if both_available:
            if abs(claude_prob - openai_prob) > _AGREEMENT_THRESHOLD:
                self.log(
                    f"Models disagree ({claude_prob:.3f} vs {openai_prob:.3f}) "
                    f"on '{market.question[:50]}' — skipping"
                )
                return None
            consensus = (claude_prob + openai_prob) / 2.0
            effective_min_edge = min_edge
        else:
            consensus = claude_prob if claude_prob is not None else openai_prob
            effective_min_edge = min_edge * _SINGLE_MODEL_EDGE_MULTIPLIER

        # ---- Edge check & signal generation ----
        # BUY YES: market underpricing YES (consensus > ask + edge)
        if consensus > best_ask + effective_min_edge:
            outcome = "YES"
            entry_price = best_ask
            token_id = yes_token_id
            gross_edge = consensus - best_ask
        # BUY NO: market overpricing YES = underpricing NO
        # NO token price ≈ 1 - best_bid(YES)
        elif consensus < best_bid - effective_min_edge:
            outcome = "NO"
            no_price = 1.0 - best_bid  # approximate NO ask price
            entry_price = no_price
            token_id = no_token_id
            gross_edge = best_bid - consensus
        else:
            return None

        fee = calc_taker_fee(entry_price)
        net_edge = gross_edge - fee

        if net_edge < effective_min_edge:
            return None

        size_usdc = self.risk.size_position(net_edge)
        if size_usdc < MIN_TRADE_USDC:
            return None

        # Build notes
        if both_available:
            notes = (
                f"[ENSEMBLE] BUY {outcome} @ {entry_price:.4f} | "
                f"claude={claude_prob:.3f} openai={openai_prob:.3f} "
                f"consensus={consensus:.3f} market={mid:.3f} edge={net_edge:.4f}"
            )
        else:
            model_name = "claude" if claude_prob is not None else "openai"
            notes = (
                f"[ENSEMBLE-SINGLE:{model_name}] BUY {outcome} @ {entry_price:.4f} | "
                f"consensus={consensus:.3f} market={mid:.3f} edge={net_edge:.4f}"
            )

        return Signal(
            strategy="ensemble",
            token_id=token_id,
            side="BUY",
            price=entry_price,
            size_usdc=size_usdc,
            edge=net_edge,
            notes=notes,
            metadata={
                "outcome": outcome,
                "claude_probability": claude_prob,
                "openai_probability": openai_prob,
                "consensus": consensus,
                "market_mid": mid,
                "condition_id": market.condition_id,
            },
        )

    # ------------------------------------------------------------------
    # Main scan entry point
    # ------------------------------------------------------------------

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        """
        Scan candidate markets and return trade signals.

        Expected context keys:
            markets   — list[Market]  (from polymarket.py)
            orderbooks — dict[str, Orderbook]  keyed by token_id
        """
        if self._anthropic_client is None and self._openai_client is None:
            self.log("No LLM clients available — returning no signals", "warning")
            return []

        markets: list[Any] = context.get("markets", [])
        orderbooks: dict[str, Any] = context.get("orderbooks", {})
        max_per_cycle: int = getattr(
            self.config, "ensemble_max_markets_per_cycle", 5
        )

        # ----------------------------------------------------------------
        # Step 1 — Filter markets
        # ----------------------------------------------------------------
        now = datetime.now(timezone.utc)
        candidates = []
        for mkt in markets:
            if not mkt.active or mkt.closed:
                continue
            if mkt.volume < _MIN_VOLUME_USDC:
                continue
            days = self._days_to_resolution(mkt.end_date_iso)
            if days is None or days > _MAX_VOLUME_DAYS or days <= 0:
                continue
            if self._find_token(mkt, "Yes") is None or self._find_token(mkt, "No") is None:
                continue
            candidates.append((mkt, days))

        # ----------------------------------------------------------------
        # Step 2 — Sort by volume desc, prefer non-cached, cap at limit
        # ----------------------------------------------------------------
        candidates.sort(key=lambda x: x[0].volume, reverse=True)

        selected: list[tuple[Any, float, _CacheEntry | None]] = []
        non_cached_count = 0

        for mkt, days in candidates:
            if len(selected) >= max_per_cycle:
                break
            cached = self._cache_get(mkt.condition_id)
            if cached is None:
                # Prefer fresh markets but don't blow over the cap
                if non_cached_count < max_per_cycle:
                    selected.append((mkt, days, None))
                    non_cached_count += 1
            else:
                # Include cached markets within cap — no API call needed
                selected.append((mkt, days, cached))

        if not selected:
            self.log("No candidate markets passed filters this cycle")
            return []

        self.log(
            f"Evaluating {len(selected)} markets "
            f"({non_cached_count} requiring LLM calls)"
        )

        # ----------------------------------------------------------------
        # Step 3 — Evaluate each market (LLM calls are parallel per market)
        # ----------------------------------------------------------------
        tasks = [
            self._evaluate_market(mkt, orderbooks, days, cached)
            for mkt, days, cached in selected
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals: list[Signal] = []
        for i, result in enumerate(results):
            mkt = selected[i][0]
            if isinstance(result, Exception):
                self.log(
                    f"Unhandled error evaluating '{mkt.question[:60]}': {result}",
                    "error",
                )
            elif result is not None:
                self.log(result.notes)
                signals.append(result)

        self.log(f"Scan complete — {len(signals)} signal(s) generated")
        return signals
