"""
AI Research utilities for the Polymarket arb bot.

Provides:
  - PerplexityClient: real-time web search (sonar / sonar-pro / sonar-reasoning-pro)
    + Agent API for multi-step autonomous research (web_search + fetch_url tools)
  - GrokClient: live X/Twitter intelligence via xAI API (OpenAI-compatible)

Both clients degrade gracefully when API keys are absent.
Usage:
    from src.utils.ai_research import perplexity, grok
    text = await perplexity.search("NBA game 4 result tonight")
    text = await perplexity.agent("Research everything about: Will BTC top $100k this week?")
    sentiment = await grok.search_x("BTC price move last 30 minutes")
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Perplexity Sonar client
# ---------------------------------------------------------------------------

class PerplexityClient:
    """
    Thin async wrapper around Perplexity's OpenAI-compatible chat/completions
    endpoint.  All models include live web search grounding.

    Models (2026 pricing):
      sonar                — $1/1M in+out  + $5/1K searches  (fast polling)
      sonar-pro            — $3/$15/1M     + $5/1K searches  (high factuality)
      sonar-reasoning-pro  — $2/$8/1M      + $5/1K searches  (CoT + live web)
      sonar-deep-research  — $2/$8/1M      + $5/1K + reasoning (exhaustive)
    """

    BASE_URL = "https://api.perplexity.ai"
    _DEFAULT_TIMEOUT = 30.0

    def __init__(self) -> None:
        self.api_key: str = os.getenv("PERPLEXITY_API_KEY", "")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, model: str = "sonar") -> str:
        """
        Single-turn grounded search.  Returns the answer text.
        Cheapest option — use for high-frequency news polling.
        """
        if not self.enabled:
            return ""
        try:
            async with httpx.AsyncClient(timeout=self._DEFAULT_TIMEOUT) as client:
                r = await client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": query}],
                        "return_citations": True,
                    },
                )
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning(f"Perplexity search error: {exc}")
            return ""

    async def research(self, query: str) -> str:
        """
        Chain-of-thought reasoning + live web.  Use before high-stakes trades.
        """
        return await self.search(query, model="sonar-reasoning-pro")

    async def deep_research(self, query: str) -> str:
        """
        Exhaustive multi-step research.  Slow (10-30s) but comprehensive.
        Use sparingly — in meta-agent or pre-session analysis only.
        """
        if not self.enabled:
            return ""
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "sonar-deep-research",
                        "messages": [{"role": "user", "content": query}],
                        "reasoning_effort": "high",
                    },
                )
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning(f"Perplexity deep_research error: {exc}")
            return ""

    async def search_structured(
        self,
        query: str,
        schema: dict[str, Any],
        model: str = "sonar-reasoning-pro",
    ) -> dict:
        """
        Return a JSON object matching *schema* (response_format JSON mode).
        schema format: {"type": "json_schema", "json_schema": {...}}
        """
        if not self.enabled:
            return {}
        try:
            async with httpx.AsyncClient(timeout=self._DEFAULT_TIMEOUT) as client:
                r = await client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": query}],
                        "response_format": schema,
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                # Strip <think>...</think> reasoning tokens if present
                if "<think>" in content:
                    end = content.find("</think>")
                    content = content[end + 8:].strip() if end != -1 else content
                return json.loads(content)
        except Exception as exc:
            logger.warning(f"Perplexity structured search error: {exc}")
            return {}


    async def agent(
        self,
        prompt: str,
        model: str = "sonar-pro",
        timeout: float = 60.0,
    ) -> str:
        """
        Deep research via sonar-pro (live web + chain-of-thought).
        The /v1/agent endpoint requires special beta access; sonar-pro via
        /chat/completions gives equivalent quality for standard accounts.
        """
        return await self.search(prompt, model=model)


# ---------------------------------------------------------------------------
# Grok (xAI) client — real-time X/Twitter intelligence
# ---------------------------------------------------------------------------

class GrokClient:
    """
    OpenAI-compatible wrapper for xAI's Grok API.
    Grok has live access to the full X/Twitter firehose — unique for detecting
    breaking sports scores, crypto moves, and political events before markets reprice.

    Docs: https://docs.x.ai/api
    Models: grok-3, grok-3-mini (fast/cheap), grok-3-fast
    Pricing (2026): ~$3/1M input, $15/1M output for grok-3; grok-3-mini much cheaper
    """

    BASE_URL = "https://api.x.ai/v1"
    _DEFAULT_TIMEOUT = 20.0

    def __init__(self) -> None:
        self.api_key: str = os.getenv("GROK_API_KEY", "")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _chat(
        self,
        prompt: str,
        model: str = "grok-3-mini",
        system: str | None = None,
        response_format: dict | None = None,
    ) -> str:
        if not self.enabled:
            return ""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if response_format:
            payload["response_format"] = response_format
        try:
            async with httpx.AsyncClient(timeout=self._DEFAULT_TIMEOUT) as client:
                r = await client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning(f"Grok API error: {exc}")
            return ""

    async def search_x(self, topic: str) -> dict:
        """
        Query X/Twitter for real-time sentiment on a topic.

        Returns:
            {
                "sentiment": "bullish" | "bearish" | "neutral",
                "key_events": ["...", ...],
                "confidence": 0.0–1.0,
                "summary": "..."
            }
        """
        prompt = (
            f"What is happening RIGHT NOW on X (Twitter) about: {topic}?\n\n"
            "Search for the most recent posts, scores, breaking news, or price moves.\n"
            "Return ONLY valid JSON:\n"
            '{"sentiment": "bullish|bearish|neutral", '
            '"key_events": ["event1", "event2"], '
            '"confidence": 0.0, '
            '"summary": "one sentence"}'
        )
        raw = await self._chat(
            prompt,
            model="grok-3-mini",
            system=(
                "You are a real-time X/Twitter intelligence agent. "
                "Search live posts and return structured JSON. No markdown."
            ),
            response_format={"type": "json_object"},
        )
        if not raw:
            return {"sentiment": "neutral", "key_events": [], "confidence": 0.0, "summary": ""}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Grok returned non-JSON: {raw[:200]}")
            return {"sentiment": "neutral", "key_events": [], "confidence": 0.0, "summary": raw[:200]}

    async def get_crypto_momentum(self, symbol: str) -> dict:
        """
        Get directional momentum for a crypto asset from X posts.
        Used by CryptoShortStrategy before snipe entries.

        Returns:
            {"direction": "up|down|sideways", "strength": 0.0–1.0, "reason": "..."}
        """
        prompt = (
            f"What is the current price direction and momentum for {symbol} "
            "based on the most recent X posts, crypto influencers, and trading chatter? "
            "Return ONLY JSON: "
            '{"direction": "up|down|sideways", "strength": 0.0, "reason": "one sentence"}'
        )
        raw = await self._chat(
            prompt,
            model="grok-3-mini",
            response_format={"type": "json_object"},
        )
        if not raw:
            return {"direction": "sideways", "strength": 0.0, "reason": ""}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"direction": "sideways", "strength": 0.0, "reason": ""}


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

perplexity = PerplexityClient()
grok = GrokClient()
