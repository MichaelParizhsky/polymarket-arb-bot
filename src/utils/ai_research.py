"""
AI Research utilities for the Polymarket arb bot.

Provides:
  - PerplexicaClient: locally-run web search via Vane (formerly Perplexica)
    running on Docker + Ollama. Drop-in replacement for PerplexityClient.
    Env vars:
      VANE_URL            — base URL (default: http://localhost:3000)
      VANE_CHAT_MODEL     — Ollama model key (default: llama3.1:8b)
      VANE_EMBEDDING_MODEL — embedding model key (default: llama3.1:8b)
  - GrokClient: live X/Twitter intelligence via xAI API (OpenAI-compatible)

Both clients degrade gracefully when unavailable.
Usage:
    from src.utils.ai_research import perplexity, grok
    text = await perplexity.search("NBA game 4 result tonight")
    text = await perplexity.research("Will BTC top $100k this week?")
    sentiment = await grok.search_x("BTC price move last 30 minutes")
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import httpx

from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Vane (Perplexica) local search client
# ---------------------------------------------------------------------------

class PerplexicaClient:
    """
    Async wrapper around the locally-running Vane (Perplexica) search engine.
    Vane runs via Docker on localhost:3000 and uses Ollama for LLM inference.

    Uses the /api/search endpoint with stream=false for simplicity.
    Provider IDs are auto-discovered from /api/providers on first use.
    """

    # llama3.1:8b on CPU takes ~60s per response; full pipeline (classify + search + synthesize)
    # takes 120-180s. Use VANE_TIMEOUT env var to tune for your hardware.
    _DEFAULT_TIMEOUT = float(os.getenv("VANE_TIMEOUT", "180"))
    _DEEP_TIMEOUT = float(os.getenv("VANE_DEEP_TIMEOUT", "300"))

    def __init__(self) -> None:
        self.base_url: str = os.getenv("VANE_URL", "http://localhost:3000").rstrip("/")
        self.chat_model_key: str = os.getenv("VANE_CHAT_MODEL", "llama3.1:8b")
        # Default embedding to Transformers (in-process WASM/ONNX, ~1s vs ~60s for Ollama)
        self.embedding_model_key: str = os.getenv("VANE_EMBEDDING_MODEL", "Xenova/all-MiniLM-L6-v2")
        self._chat_provider_id: str | None = None
        self._embedding_provider_id: str | None = None
        self._available: bool | None = None  # None = not yet checked

    async def _discover_providers(self) -> bool:
        """
        Fetch /api/providers and cache the provider IDs for the configured models.
        Returns True if discovery succeeded.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/providers")
                r.raise_for_status()
                providers: list[dict] = r.json().get("providers", [])

            for provider in providers:
                pid = provider.get("id", "")
                chat_keys = [m["key"] for m in provider.get("chatModels", [])]
                # Match embedding by key OR by name (some providers use display name)
                embed_models = provider.get("embeddingModels", [])
                embed_keys = [m["key"] for m in embed_models]
                embed_names = [m["name"] for m in embed_models]
                if self.chat_model_key in chat_keys:
                    self._chat_provider_id = pid
                if (self.embedding_model_key in embed_keys
                        or self.embedding_model_key in embed_names):
                    self._embedding_provider_id = pid

            if self._chat_provider_id and self._embedding_provider_id:
                logger.info(
                    f"Vane providers discovered — chat: {self._chat_provider_id[:8]}…, "
                    f"embed: {self._embedding_provider_id[:8]}…"
                )
                return True

            logger.warning(
                f"Vane: model '{self.chat_model_key}' not found in any provider. "
                "Run `ollama pull llama3.1:8b` and restart Vane."
            )
            return False
        except Exception as exc:
            logger.warning(f"Vane provider discovery failed: {exc}")
            return False

    @property
    def enabled(self) -> bool:
        """True once providers have been discovered successfully."""
        return self._available is True

    async def _ensure_ready(self) -> bool:
        """Discover providers on first use; cache the result."""
        if self._available is None:
            self._available = await self._discover_providers()
        return self._available

    def _build_payload(
        self,
        query: str,
        mode: str = "balanced",
        sources: list[str] | None = None,
        system_instructions: str = "",
    ) -> dict[str, Any]:
        return {
            "query": query,
            "optimizationMode": mode,
            "sources": sources or ["web"],
            "history": [],
            "chatModel": {
                "providerId": self._chat_provider_id,
                "key": self.chat_model_key,
            },
            "embeddingModel": {
                "providerId": self._embedding_provider_id,
                "key": self.embedding_model_key,
            },
            "systemInstructions": system_instructions,
        }

    async def _query(
        self,
        query: str,
        mode: str = "balanced",
        sources: list[str] | None = None,
        system_instructions: str = "",
        timeout: float | None = None,
    ) -> str:
        if not await self._ensure_ready():
            return ""
        try:
            payload = self._build_payload(query, mode, sources, system_instructions)
            payload["stream"] = True  # use streaming to avoid non-streaming timeout

            chunks: list[str] = []
            effective_timeout = timeout or self._DEFAULT_TIMEOUT

            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/search",
                    json=payload,
                ) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        event_type = event.get("type")
                        if event_type == "response":
                            chunks.append(event.get("data", ""))
                        elif event_type == "done":
                            break
                        elif event_type == "error":
                            logger.warning(f"Vane stream error: {event.get('data')}")
                            break

            return "".join(chunks)
        except Exception as exc:
            logger.warning(f"Vane search error: {exc}")
            return ""

    async def search(self, query: str, model: str = "sonar") -> str:
        """
        Fast web search. `model` param is ignored (kept for API compatibility
        with callers that passed a Perplexity model name).
        Uses 'speed' optimization for lowest latency.
        """
        return await self._query(query, mode="speed", sources=["web"])

    async def research(self, query: str) -> str:
        """Deeper web search with balanced quality/speed."""
        return await self._query(query, mode="balanced", sources=["web"])

    async def deep_research(self, query: str) -> str:
        """
        Exhaustive research using quality mode. Slow but comprehensive.
        Use sparingly — meta-agent or pre-session analysis only.
        """
        return await self._query(
            query,
            mode="quality",
            sources=["web"],
            timeout=self._DEEP_TIMEOUT,
        )

    async def search_structured(
        self,
        query: str,
        schema: dict[str, Any],
        model: str = "sonar-reasoning-pro",
    ) -> dict:
        """
        Return a JSON object. Since Vane doesn't have native JSON-mode,
        we append instructions to force JSON output and parse the response.
        `schema` param is accepted for API compatibility but used only as
        a hint in the system instructions.
        """
        schema_hint = ""
        if "json_schema" in schema:
            inner = schema["json_schema"]
            schema_hint = f"\nExpected JSON schema:\n{json.dumps(inner, indent=2)}"

        system = (
            "You are a research assistant. Return ONLY valid JSON with no markdown, "
            "no code fences, no explanations. Output raw JSON only." + schema_hint
        )
        raw = await self._query(
            query,
            mode="balanced",
            sources=["web"],
            system_instructions=system,
        )
        if not raw:
            return {}
        # Strip any markdown fences or <think> blocks if model added them
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw).rstrip("` \n")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract first JSON object from the response
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            logger.warning(f"Vane structured response is not valid JSON: {raw[:200]}")
            return {}

    async def agent(
        self,
        prompt: str,
        model: str = "sonar-pro",
        timeout: float = 60.0,
    ) -> str:
        """Multi-step research using quality mode. `model` param ignored."""
        return await self._query(
            prompt,
            mode="quality",
            sources=["web"],
            timeout=timeout,
        )


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
# Ollama local embedding client
# ---------------------------------------------------------------------------

class OllamaEmbeddingClient:
    """
    Async wrapper for Ollama's local embedding API using nomic-embed-text.
    ~274 MB model, ~200ms per embedding on CPU.

    Requires: ollama pull nomic-embed-text

    Used by NewsMonitor for semantic headline ranking — replaces keyword
    overlap scoring with cosine similarity, catching synonyms and phrasing
    variations that keyword matching misses.

    Env vars:
      OLLAMA_URL          — base URL (default: http://localhost:11434)
      OLLAMA_EMBED_MODEL  — model name (default: nomic-embed-text)
    """

    _DEFAULT_TIMEOUT = 30.0

    def __init__(self) -> None:
        self.base_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
        self.model: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self._available: bool | None = None
        # LRU-style text → vector cache (bounded at 2000 entries)
        self._cache: dict[str, list[float]] = {}
        self._cache_max: int = 2000

    async def _check_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                models = [m["name"] for m in r.json().get("models", [])]
                base = self.model.split(":")[0]
                found = any(base in m for m in models)
                if not found:
                    logger.warning(
                        f"Ollama: '{self.model}' not found. "
                        f"Run `ollama pull {self.model}` for semantic news ranking."
                    )
                return found
        except Exception as exc:
            logger.debug(f"Ollama embedding unavailable: {exc}")
            return False

    @property
    def enabled(self) -> bool:
        return self._available is True

    async def _ensure_ready(self) -> bool:
        if self._available is None:
            self._available = await self._check_available()
        return self._available

    async def embed(self, text: str) -> list[float]:
        """Return embedding vector for text. Returns [] on failure."""
        if not await self._ensure_ready():
            return []
        if text in self._cache:
            return self._cache[text]
        try:
            async with httpx.AsyncClient(timeout=self._DEFAULT_TIMEOUT) as client:
                r = await client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                r.raise_for_status()
                vec: list[float] = r.json().get("embedding", [])
        except Exception as exc:
            logger.debug(f"Ollama embed error: {exc}")
            return []
        if len(self._cache) >= self._cache_max:
            del self._cache[next(iter(self._cache))]
        self._cache[text] = vec
        return vec

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        """Cosine similarity in [0, 1]. Returns 0.0 if either vector is empty."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

perplexity = PerplexicaClient()
grok = GrokClient()
embeddings = OllamaEmbeddingClient()
