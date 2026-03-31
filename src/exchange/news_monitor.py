"""
NewsMonitor — background news polling for the event-driven strategy.

Polls free news sources every 5 minutes (configurable):
  - Vane (Perplexica) local search (primary, when Docker container is running)
  - Google News RSS (free, no API key required; always runs as fallback)
  - CryptoPanic free endpoint (attempted without auth; skipped on failure)

Maintains a rolling 6-hour headline cache and provides keyword-scored
retrieval via get_relevant_news().

Usage:
    monitor = NewsMonitor(poll_interval=300)
    await monitor.start()
    ...
    news = monitor.get_relevant_news("Will Bitcoin exceed $100k in 2026?")
    context = await monitor.get_perplexity_context("Will the Fed cut rates?")
    await monitor.stop()
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

from src.utils.ai_research import embeddings, perplexity
from src.utils.logger import logger

try:
    import feedparser
    _FEEDPARSER_AVAILABLE = True
except ImportError:
    _FEEDPARSER_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)
_CRYPTOPANIC_URL = (
    "https://cryptopanic.com/api/free/v1/posts/?auth_token=&public=true&filter=important"
)
_CACHE_MAX_AGE_SECONDS = 6 * 3600   # 6 hours
_REQUEST_TIMEOUT = 15.0              # seconds per HTTP request

# Perplexity polling: one query per category per poll cycle
_PERPLEXITY_CATEGORIES = [
    "crypto markets Bitcoin Ethereum price moves last 2 hours",
    "sports scores NBA NHL NFL MLB last 2 hours",
    "US politics elections breaking news last 2 hours",
    "global financial markets earnings Fed rates last 2 hours",
]

# Cache TTL for get_perplexity_context() results
_PERPLEXITY_CONTEXT_TTL = 300  # 5 minutes

# Stopwords to strip before keyword scoring
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "will", "would", "could", "should", "may", "might", "do", "does", "did",
    "have", "has", "had", "not", "no", "it", "its", "this", "that", "these",
    "those", "if", "as", "up", "out", "over", "into", "than", "then", "so",
    "can", "about", "after", "before", "during", "through", "between", "what",
    "which", "who", "whom", "when", "where", "why", "how", "all", "any",
    "each", "every", "both", "either", "more", "most", "other", "such",
    "while", "because", "since", "until", "against", "under", "above", "per",
})

# Sentiment word lists
_BULLISH_WORDS = frozenset({
    "surge", "surges", "surging", "rally", "rallies", "rallying", "rise",
    "rises", "rising", "gain", "gains", "jump", "jumps", "soar", "soars",
    "soaring", "breakout", "breakthrough", "record", "high", "bull",
    "bullish", "approve", "approves", "approval", "win", "wins", "winning",
    "beat", "beats", "exceeds", "positive", "growth", "grows", "expand",
    "strong", "stronger", "strengthen", "boom", "booming", "upgrade",
    "outperform", "beat expectations", "higher", "up", "above",
})
_BEARISH_WORDS = frozenset({
    "drop", "drops", "dropping", "fall", "falls", "falling", "decline",
    "declines", "declining", "crash", "crashes", "crashing", "plunge",
    "plunges", "plunging", "sink", "sinks", "sinking", "slump", "slumps",
    "bear", "bearish", "reject", "rejects", "rejection", "ban", "bans",
    "banned", "fail", "fails", "failing", "failure", "miss", "misses",
    "below", "worse", "worsen", "weaken", "weak", "contract", "contraction",
    "recession", "concern", "warning", "risk", "loss", "losses", "down",
    "downgrade", "underperform", "disappoints", "disappointing", "negative",
    "lower", "cut", "cuts",
})

# Crypto-specific search terms to use for the crypto news RSS query
_CRYPTO_RSS_QUERY = "bitcoin OR ethereum OR crypto OR cryptocurrency OR BTC OR ETH"


# ---------------------------------------------------------------------------
# NewsMonitor
# ---------------------------------------------------------------------------

class NewsMonitor:
    """
    Background news monitor.  Call start() to begin polling, stop() to end.
    Thread-safe for reading; the internal cache is replaced atomically.

    When PERPLEXITY_API_KEY is set, Perplexity Sonar is used as the primary
    news source (polled every poll_interval seconds across 4 market categories).
    Google News RSS always runs as fallback regardless.
    """

    def __init__(self, poll_interval: int = 300) -> None:
        self._poll_interval = poll_interval
        self._cache: list[dict] = []          # list of headline dicts
        self._task: asyncio.Task | None = None
        self._running = False
        self._client: httpx.AsyncClient | None = None

        # Cache for get_perplexity_context(): {question_hash -> (result, expires_at)}
        self._perplexity_cache: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background polling loop."""
        if self._running:
            return
        self._running = True
        self._client = httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": "polymarket-arb-bot/1.0 (news-monitor)"},
            follow_redirects=True,
        )
        # Do an immediate fetch before scheduling the loop
        await self._fetch_all()
        self._task = asyncio.create_task(self._poll_loop(), name="news_monitor")

        if perplexity.enabled:
            logger.info(
                f"[NewsMonitor] Started — poll_interval={self._poll_interval}s, "
                f"primary=perplexity_sonar, fallback=google_rss, "
                f"feedparser={'available' if _FEEDPARSER_AVAILABLE else 'fallback XML'}"
            )
        else:
            logger.info(
                f"[NewsMonitor] Started — poll_interval={self._poll_interval}s, "
                f"primary=google_rss (no PERPLEXITY_API_KEY), "
                f"feedparser={'available' if _FEEDPARSER_AVAILABLE else 'fallback XML'}"
            )

    async def stop(self) -> None:
        """Stop background polling loop and close HTTP client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("[NewsMonitor] Stopped.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_relevant_news(self, question: str, max_results: int = 5) -> list[dict]:
        """
        Return up to max_results headlines most relevant to the market question.
        Uses keyword overlap scoring (sync, always available).
        For semantic ranking, use get_relevant_news_semantic() from async context.

        Each item: {"title", "published", "source", "url", "sentiment"}
        """
        now = time.time()
        cutoff = now - _CACHE_MAX_AGE_SECONDS
        recent = [h for h in self._cache if h.get("_ts", 0) >= cutoff]

        if not recent or not question:
            return []

        keywords = self._extract_keywords(question)
        if not keywords:
            return []

        scored = [
            (self._keyword_score(h["title"], keywords), h)
            for h in recent
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [h for score, h in scored if score > 0][:max_results]

    async def get_relevant_news_semantic(
        self, question: str, max_results: int = 5
    ) -> list[dict]:
        """
        Semantic version of get_relevant_news using nomic-embed-text via Ollama.
        Falls back to keyword scoring when Ollama is unavailable.

        Cosine similarity against pre-embedded headline titles catches synonyms
        and phrasing variations that keyword overlap misses (e.g. "Fed cuts"
        matches "central bank lowers rates").
        """
        now = time.time()
        cutoff = now - _CACHE_MAX_AGE_SECONDS
        recent = [h for h in self._cache if h.get("_ts", 0) >= cutoff]

        if not recent or not question:
            return []

        # Try semantic ranking if embeddings available and headlines are pre-embedded
        if embeddings.enabled:
            query_vec = await embeddings.embed(question)
            if query_vec:
                scored = []
                for h in recent:
                    h_vec = h.get("_emb")
                    if h_vec:
                        sim = embeddings.cosine(query_vec, h_vec)
                    else:
                        # Headline not yet embedded — fall back to keyword for this one
                        kw = self._extract_keywords(question)
                        sim = self._keyword_score(h["title"], kw) / max(len(kw), 1) * 0.5
                    scored.append((sim, h))
                scored.sort(key=lambda x: x[0], reverse=True)
                # Minimum similarity threshold — avoids returning completely unrelated news
                return [h for sim, h in scored if sim > 0.25][:max_results]

        # Fallback: keyword scoring
        return self.get_relevant_news(question, max_results)

    async def get_perplexity_context(self, question: str) -> str:
        """
        Get targeted real-time context for a specific market question.
        Uses sonar-reasoning-pro for high-quality analysis.
        Returns empty string if Perplexity is not configured.

        Used by EventDrivenStrategy and SwarmPredictionStrategy before trades.
        Rate-limited: caches results per question for 5 minutes to avoid
        hammering the API on every cycle.
        """
        if not perplexity.enabled:
            return ""

        cache_key = hashlib.sha256(question.encode()).hexdigest()
        now = time.time()

        cached = self._perplexity_cache.get(cache_key)
        if cached is not None:
            result, expires_at = cached
            if now < expires_at:
                return result

        try:
            result = await perplexity.research(question)
        except Exception as exc:
            logger.debug(f"[NewsMonitor] get_perplexity_context failed: {exc}")
            return ""

        self._perplexity_cache[cache_key] = (result, now + _PERPLEXITY_CONTEXT_TTL)
        return result

    # ------------------------------------------------------------------
    # Internal polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Runs indefinitely, fetching news every poll_interval seconds."""
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if self._running:
                    await self._fetch_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[NewsMonitor] Poll loop error: {exc}")

    async def _fetch_all(self) -> None:
        """Fetch news from all sources and merge into the cache."""
        results: list[dict] = []

        # Always run RSS sources
        general_task = asyncio.create_task(
            self._fetch_general_news(_CRYPTO_RSS_QUERY)
        )
        crypto_task = asyncio.create_task(self._fetch_crypto_news())

        tasks: list[asyncio.Task] = [general_task, crypto_task]

        # Run Perplexity polling as an additional source when configured
        if perplexity.enabled:
            perplexity_task = asyncio.create_task(self._poll_perplexity())
            tasks.append(perplexity_task)

        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        for outcome in gathered:
            if isinstance(outcome, list):
                results.extend(outcome)

        if results:
            # Merge with existing cache, de-duplicate by URL
            now = time.time()
            existing_urls = {h["url"] for h in self._cache}
            new_items = [r for r in results if r["url"] not in existing_urls]
            combined = self._cache + new_items

            # Evict entries older than 6 hours
            cutoff = now - _CACHE_MAX_AGE_SECONDS
            self._cache = [h for h in combined if h.get("_ts", 0) >= cutoff]

            logger.debug(
                f"[NewsMonitor] Cache updated: {len(self._cache)} headlines "
                f"({len(new_items)} new)"
            )

            # Background-embed new headline titles for semantic ranking.
            # Fire-and-forget: failures are silently ignored; keyword fallback always works.
            if new_items and embeddings.enabled:
                asyncio.create_task(
                    self._embed_new_headlines(new_items),
                    name="news_embed",
                )

    async def _embed_new_headlines(self, headlines: list[dict]) -> None:
        """
        Embed headline titles via Ollama and store the vector in-place as '_emb'.
        Runs as a background task after each cache update.
        Throttled to avoid blocking the event loop: one embed call at a time.
        """
        for h in headlines:
            title = h.get("title", "")
            if not title or "_emb" in h:
                continue
            try:
                vec = await embeddings.embed(title)
                if vec:
                    h["_emb"] = vec
            except Exception:
                pass  # never crash the poll loop

    # ------------------------------------------------------------------
    # Perplexity polling
    # ------------------------------------------------------------------

    async def _poll_perplexity(self) -> list[dict]:
        """
        Poll Perplexity Sonar for each active market category.
        Runs at most 4 calls per cycle (one per category).
        Failures per category are caught individually and skipped silently.
        Returns a flat list of synthetic NewsItem dicts compatible with the cache.
        """
        if not perplexity.enabled:
            return []

        results: list[dict] = []

        for category_query in _PERPLEXITY_CATEGORIES:
            try:
                raw = await perplexity.search(category_query, model="sonar")
                if not raw:
                    continue

                items = self._parse_perplexity_response(raw, category_query)
                results.extend(items)

            except Exception as exc:
                logger.debug(
                    f"[NewsMonitor] Perplexity poll failed for "
                    f"'{category_query[:40]}...': {exc}"
                )
                # Fall through silently; RSS will cover this category

        return results

    def _parse_perplexity_response(self, raw_text: str, category: str) -> list[dict]:
        """
        Convert a Perplexity free-text response into synthetic NewsItem dicts
        compatible with the existing headline cache.

        Each non-empty sentence/line becomes one headline. Duplicate-safe URLs
        are built from a hash of the content so de-duplication in _fetch_all works.
        """
        results: list[dict] = []
        now = time.time()

        # Split on newlines and sentence boundaries; filter short fragments
        import re
        lines = re.split(r"\n+|(?<=[.!?])\s+", raw_text)

        for line in lines:
            line = line.strip()
            # Strip common markdown artifacts
            line = re.sub(r"^\s*[\*\-•]\s*", "", line)
            line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)

            if len(line) < 20:
                continue

            # Build a stable synthetic URL so de-duplication works across polls
            url_hash = hashlib.md5(line.encode()).hexdigest()
            synthetic_url = f"perplexity://sonar/{url_hash}"

            results.append(self._build_headline(
                title=line[:300],
                url=synthetic_url,
                published="",   # _parse_published_ts returns now() for empty string
                source="Perplexity Sonar",
            ))

        return results

    # ------------------------------------------------------------------
    # Source fetchers
    # ------------------------------------------------------------------

    async def _fetch_general_news(self, query: str) -> list[dict]:
        """
        Fetch from Google News RSS.  Uses feedparser if available,
        falls back to stdlib xml.etree.ElementTree.
        """
        if not self._client:
            return []
        try:
            encoded = urllib.parse.quote(query)
            url = _GOOGLE_NEWS_RSS.format(query=encoded)
            response = await self._client.get(url)
            response.raise_for_status()
            content = response.text

            if _FEEDPARSER_AVAILABLE:
                return self._parse_rss_feedparser(content, source="Google News")
            else:
                return self._parse_rss_xml(content, source="Google News")

        except Exception as exc:
            logger.debug(f"[NewsMonitor] Google News RSS fetch failed: {exc}")
            return []

    async def _fetch_crypto_news(self) -> list[dict]:
        """
        Attempt to fetch from CryptoPanic free endpoint.
        Skips gracefully on any failure (auth required, rate limit, etc.).
        Falls back to a second Google News RSS query for crypto topics.
        """
        if not self._client:
            return []

        # Try CryptoPanic first
        try:
            response = await self._client.get(_CRYPTOPANIC_URL)
            if response.status_code == 200:
                data = response.json()
                return self._parse_cryptopanic(data)
        except Exception as exc:
            logger.debug(f"[NewsMonitor] CryptoPanic fetch skipped: {exc}")

        # Fallback: second Google News RSS query focused on crypto prices
        try:
            fallback_query = "bitcoin price ethereum crypto market"
            return await self._fetch_general_news(fallback_query)
        except Exception as exc:
            logger.debug(f"[NewsMonitor] Crypto news fallback failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_rss_feedparser(self, content: str, source: str) -> list[dict]:
        """Parse RSS XML using feedparser."""
        results: list[dict] = []
        try:
            feed = feedparser.parse(content)
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                url = getattr(entry, "link", "").strip()
                published = getattr(entry, "published", "").strip()
                source_name = source
                if hasattr(entry, "source") and hasattr(entry.source, "title"):
                    source_name = entry.source.title

                if not title or not url:
                    continue

                results.append(self._build_headline(
                    title=title,
                    url=url,
                    published=published,
                    source=source_name,
                ))
        except Exception as exc:
            logger.debug(f"[NewsMonitor] feedparser parse error: {exc}")
        return results

    def _parse_rss_xml(self, content: str, source: str) -> list[dict]:
        """Parse RSS XML using stdlib ElementTree (feedparser fallback)."""
        results: list[dict] = []
        try:
            root = ET.fromstring(content)
            channel = root.find("channel")
            if channel is None:
                return results

            for item in channel.findall("item"):
                title_el = item.find("title")
                link_el = item.find("link")
                pub_el = item.find("pubDate")
                source_el = item.find("source")

                title = title_el.text.strip() if title_el is not None and title_el.text else ""
                url = link_el.text.strip() if link_el is not None and link_el.text else ""
                published = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
                source_name = (
                    source_el.text.strip()
                    if source_el is not None and source_el.text
                    else source
                )

                if not title or not url:
                    continue

                results.append(self._build_headline(
                    title=title,
                    url=url,
                    published=published,
                    source=source_name,
                ))
        except ET.ParseError as exc:
            logger.debug(f"[NewsMonitor] XML parse error: {exc}")
        except Exception as exc:
            logger.debug(f"[NewsMonitor] RSS XML parse error: {exc}")
        return results

    def _parse_cryptopanic(self, data: dict) -> list[dict]:
        """Parse CryptoPanic JSON response."""
        results: list[dict] = []
        try:
            posts = data.get("results", [])
            for post in posts:
                title = post.get("title", "").strip()
                url = post.get("url", "").strip()
                published = post.get("published_at", "").strip()
                source_info = post.get("source", {})
                source_name = source_info.get("title", "CryptoPanic") if source_info else "CryptoPanic"

                if not title or not url:
                    continue

                results.append(self._build_headline(
                    title=title,
                    url=url,
                    published=published,
                    source=source_name,
                ))
        except Exception as exc:
            logger.debug(f"[NewsMonitor] CryptoPanic parse error: {exc}")
        return results

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _build_headline(
        self, title: str, url: str, published: str, source: str
    ) -> dict:
        """Construct a normalized headline dict with sentiment and timestamp."""
        return {
            "title": title,
            "published": published,
            "source": source,
            "url": url,
            "sentiment": self._classify_sentiment(title),
            "_ts": self._parse_published_ts(published),
        }

    def _parse_published_ts(self, published: str) -> float:
        """
        Try to parse a published date string into a unix timestamp.
        Returns current time on failure (safe default — headline is treated as fresh).
        """
        if not published:
            return time.time()
        # Common RSS formats
        formats = [
            "%a, %d %b %Y %H:%M:%S %z",   # RFC 2822: "Mon, 01 Jan 2026 12:00:00 +0000"
            "%a, %d %b %Y %H:%M:%S GMT",   # RFC 2822 GMT variant
            "%Y-%m-%dT%H:%M:%S%z",         # ISO 8601
            "%Y-%m-%dT%H:%M:%SZ",          # ISO 8601 UTC
            "%Y-%m-%d %H:%M:%S",           # Simple datetime
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(published, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
        # Last resort: return now so the headline isn't evicted
        return time.time()

    def _classify_sentiment(self, headline: str) -> str:
        """
        Simple word-list sentiment classifier.
        Returns "positive", "negative", or "neutral".
        """
        words = set(headline.lower().split())
        bullish_hits = len(words & _BULLISH_WORDS)
        bearish_hits = len(words & _BEARISH_WORDS)
        if bullish_hits > bearish_hits:
            return "positive"
        if bearish_hits > bullish_hits:
            return "negative"
        return "neutral"

    def _extract_keywords(self, question: str) -> set[str]:
        """
        Extract meaningful keywords from a market question by stripping
        punctuation and removing stopwords.
        """
        import re
        tokens = re.sub(r"[^\w\s]", " ", question.lower()).split()
        return {t for t in tokens if t not in _STOPWORDS and len(t) > 2}

    def _keyword_score(self, headline: str, keywords: set[str]) -> float:
        """
        Score a headline by keyword overlap with the market question.
        Returns a float 0.0–1.0 (fraction of keywords that appear in headline).
        """
        if not keywords:
            return 0.0
        headline_lower = headline.lower()
        hits = sum(1 for kw in keywords if kw in headline_lower)
        return hits / len(keywords)
