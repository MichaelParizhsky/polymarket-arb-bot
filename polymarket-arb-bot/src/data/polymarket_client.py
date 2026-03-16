"""
data/polymarket_client.py — Async Polymarket API client
Fetches markets, prices, and order book data from Gamma + CLOB APIs.
"""
from __future__ import annotations
import asyncio
import os
from datetime import datetime
from typing import List, Optional, Dict, Any

import aiohttp
from loguru import logger

from src.models import Market, Outcome


GAMMA_API  = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com")
CLOB_API   = os.getenv("POLYMARKET_CLOB_API",  "https://clob.polymarket.com")


class PolymarketClient:
    """
    Async client for the Polymarket public APIs.
    Gamma API  → market metadata, categories, events
    CLOB API   → order book prices, spreads
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._price_cache: Dict[str, float] = {}
        self._cache_ts:    Dict[str, datetime] = {}

    # ── Market fetching ───────────────────────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active_only: bool = True,
        category: Optional[str] = None,
    ) -> List[Market]:
        """Fetch a page of markets from Gamma API."""
        params: Dict[str, Any] = {
            "limit":  limit,
            "offset": offset,
            "closed": "false" if active_only else "true",
        }
        if category:
            params["tag"] = category

        try:
            async with self.session.get(
                f"{GAMMA_API}/markets", params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                raw = await resp.json()

            markets = []
            items = raw if isinstance(raw, list) else raw.get("markets", [])
            for item in items:
                m = self._parse_market(item)
                if m:
                    markets.append(m)
            return markets

        except Exception as e:
            logger.warning(f"[Gamma] Failed to fetch markets: {e}")
            return []

    async def get_all_active_markets(self, max_pages: int = 10) -> List[Market]:
        """Paginate through all active markets."""
        all_markets: List[Market] = []
        limit = 100
        for page in range(max_pages):
            batch = await self.get_markets(limit=limit, offset=page * limit)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < limit:
                break
            await asyncio.sleep(0.2)   # gentle rate limiting
        logger.info(f"[Gamma] Fetched {len(all_markets)} active markets")
        return all_markets

    async def get_market_by_id(self, market_id: str) -> Optional[Market]:
        try:
            async with self.session.get(
                f"{GAMMA_API}/markets/{market_id}",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                resp.raise_for_status()
                return self._parse_market(await resp.json())
        except Exception as e:
            logger.warning(f"[Gamma] Failed to fetch market {market_id}: {e}")
            return None

    # ── Order book / prices ───────────────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> Optional[Dict]:
        """Fetch CLOB order book for a specific outcome token."""
        try:
            async with self.session.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            logger.debug(f"[CLOB] Order book fetch failed for {token_id}: {e}")
            return None

    async def enrich_market_with_clob(self, market: Market) -> Market:
        """Attach best bid/ask from CLOB to each outcome."""
        tasks = [self.get_order_book(o.token_id) for o in market.outcomes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for outcome, book in zip(market.outcomes, results):
            if isinstance(book, dict):
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids:
                    outcome.best_bid = float(bids[0].get("price", outcome.price))
                if asks:
                    outcome.best_ask = float(asks[0].get("price", outcome.price))
        return market

    async def get_midpoint_price(self, token_id: str) -> Optional[float]:
        """Return midpoint price for a token (cached 2s)."""
        now = datetime.utcnow()
        cached_at = self._cache_ts.get(token_id)
        if cached_at and (now - cached_at).total_seconds() < 2:
            return self._price_cache.get(token_id)

        book = await self.get_order_book(token_id)
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None

        mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
        self._price_cache[token_id] = mid
        self._cache_ts[token_id]    = now
        return mid

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_market(self, raw: Dict) -> Optional[Market]:
        try:
            outcomes_raw = raw.get("tokens", raw.get("outcomes", []))
            if not outcomes_raw or len(outcomes_raw) < 2:
                return None

            outcomes = []
            for o in outcomes_raw:
                # Gamma API uses "price" or "outcome_price"
                price_val = float(o.get("price", o.get("outcome_price", 0.5)))
                outcomes.append(Outcome(
                    token_id  = str(o.get("token_id", o.get("id", ""))),
                    name      = str(o.get("outcome", o.get("name", "Unknown"))),
                    price     = price_val,
                    volume_24h= float(o.get("volume", 0) or 0),
                ))

            end_date = None
            if raw.get("end_date_iso"):
                try:
                    end_date = datetime.fromisoformat(raw["end_date_iso"].replace("Z", "+00:00"))
                except Exception:
                    pass

            return Market(
                market_id   = str(raw.get("id", raw.get("condition_id", ""))),
                condition_id= str(raw.get("condition_id", raw.get("id", ""))),
                question    = str(raw.get("question", "Unknown market")),
                category    = raw.get("category", raw.get("tag", None)),
                outcomes    = outcomes,
                end_date    = end_date,
                active      = not raw.get("closed", False),
                liquidity   = float(raw.get("liquidity", 0) or 0),
                volume_24h  = float(raw.get("volume24hr", raw.get("volume", 0)) or 0),
            )
        except Exception as e:
            logger.debug(f"[Parser] Failed to parse market: {e} — raw keys: {list(raw.keys())}")
            return None
