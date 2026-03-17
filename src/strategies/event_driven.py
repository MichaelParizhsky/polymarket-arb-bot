"""
Strategy 5: Event-Driven Trading.

Major events (Fed meetings, elections, sports finals, earnings) cause large
price dislocations on Polymarket. This strategy:
  1. Maintains a calendar of upcoming major events
  2. Within a configurable window before/during an event, increases position
     size multiplier
  3. Scans for markets related to the event using keyword matching
  4. Applies a looser edge threshold during event windows (to capture
     high-conviction moves during volatility)
  5. Generates signals to trade event-related markets with higher conviction

Custom events can be loaded from logs/custom_events.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.strategies.base import BaseStrategy, Signal
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CalendarEvent:
    name: str
    event_time: float          # unix timestamp
    keywords: list[str]        # keywords to match Polymarket questions
    size_multiplier: float     # 1.0 = normal, 2.0 = double size
    category: str              # "macro", "crypto", "sports", "politics"
    window_before_hours: float = 2.0   # hours before event to activate
    window_after_hours: float = 1.0    # hours after event to keep active


# ---------------------------------------------------------------------------
# Calendar builder
# ---------------------------------------------------------------------------

def _unix(iso_date: str) -> float:
    """Convert an ISO date string (YYYY-MM-DD) to a UTC unix timestamp at noon."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(
        hour=14, minute=0, second=0, tzinfo=timezone.utc  # 14:00 UTC = typical release time
    )
    return dt.timestamp()


def _unix_hhmm(iso_date: str, hour: int, minute: int) -> float:
    """Convert an ISO date + time to UTC unix timestamp."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=0, tzinfo=timezone.utc
    )
    return dt.timestamp()


def get_default_calendar() -> list[CalendarEvent]:
    """
    Build a default calendar of major market-moving events.
    All timestamps are in UTC. Dates are hardcoded for the near-term horizon
    relative to the deployment date (around 2026-03).
    """
    events: list[CalendarEvent] = []

    # ------------------------------------------------------------------
    # Fed FOMC meetings — announcement typically at 18:00 UTC (2pm ET)
    # Roughly every 6 weeks
    # ------------------------------------------------------------------
    fomc_dates = [
        ("2026-03-19", 18, 0),
        ("2026-05-07", 18, 0),
        ("2026-06-18", 18, 0),
        ("2026-07-30", 18, 0),
    ]
    for date_str, h, m in fomc_dates:
        events.append(CalendarEvent(
            name=f"FOMC Meeting {date_str}",
            event_time=_unix_hhmm(date_str, h, m),
            keywords=["fed", "fomc", "interest rate", "federal reserve", "rate hike",
                      "rate cut", "basis points", "powell", "monetary policy"],
            size_multiplier=2.0,
            category="macro",
            window_before_hours=2.0,
            window_after_hours=2.0,
        ))

    # ------------------------------------------------------------------
    # US CPI releases — typically 12:30 UTC (8:30am ET) on release day
    # ------------------------------------------------------------------
    cpi_dates = [
        ("2026-04-10", 12, 30),
        ("2026-05-13", 12, 30),
    ]
    for date_str, h, m in cpi_dates:
        events.append(CalendarEvent(
            name=f"US CPI Release {date_str}",
            event_time=_unix_hhmm(date_str, h, m),
            keywords=["cpi", "inflation", "consumer price", "core inflation",
                      "price index", "pce", "deflation"],
            size_multiplier=1.8,
            category="macro",
            window_before_hours=1.0,
            window_after_hours=1.5,
        ))

    # ------------------------------------------------------------------
    # US Non-Farm Payrolls / Jobs Report — first Friday of each month
    # 12:30 UTC (8:30am ET)
    # ------------------------------------------------------------------
    nfp_dates = [
        ("2026-04-03", 12, 30),
        ("2026-05-01", 12, 30),
        ("2026-06-05", 12, 30),
        ("2026-07-10", 12, 30),
    ]
    for date_str, h, m in nfp_dates:
        events.append(CalendarEvent(
            name=f"US Jobs Report (NFP) {date_str}",
            event_time=_unix_hhmm(date_str, h, m),
            keywords=["nfp", "jobs report", "non-farm payroll", "payrolls",
                      "unemployment rate", "labor market", "jobs added"],
            size_multiplier=1.6,
            category="macro",
            window_before_hours=1.0,
            window_after_hours=1.5,
        ))

    # ------------------------------------------------------------------
    # Crypto volatility events — ongoing high-attention periods
    # Bitcoin / Ethereum ETF decisions, major protocol upgrades
    # ------------------------------------------------------------------
    events.append(CalendarEvent(
        name="Bitcoin Halving Anniversary / Volatility Window",
        event_time=_unix_hhmm("2026-04-20", 12, 0),  # ~1 year after 2024 halving
        keywords=["bitcoin", "btc", "halving", "crypto", "blockchain"],
        size_multiplier=1.5,
        category="crypto",
        window_before_hours=24.0,
        window_after_hours=24.0,
    ))

    events.append(CalendarEvent(
        name="Bitcoin ETF Monthly Options Expiry",
        event_time=_unix_hhmm("2026-04-17", 16, 0),  # 3rd Friday of month
        keywords=["bitcoin", "btc", "etf", "options", "crypto"],
        size_multiplier=1.4,
        category="crypto",
        window_before_hours=2.0,
        window_after_hours=1.0,
    ))

    # ------------------------------------------------------------------
    # Major election dates
    # ------------------------------------------------------------------
    events.append(CalendarEvent(
        name="US Midterm Elections 2026",
        event_time=_unix_hhmm("2026-11-03", 23, 0),  # polls close ~11pm UTC
        keywords=["midterm", "election", "congress", "senate", "house",
                  "democrat", "republican", "ballot", "vote", "polling"],
        size_multiplier=2.0,
        category="politics",
        window_before_hours=6.0,
        window_after_hours=12.0,
    ))

    events.append(CalendarEvent(
        name="US State Primary Elections 2026",
        event_time=_unix_hhmm("2026-06-02", 23, 0),
        keywords=["primary", "election", "senate", "governor", "democrat",
                  "republican", "ballot", "vote"],
        size_multiplier=1.5,
        category="politics",
        window_before_hours=3.0,
        window_after_hours=6.0,
    ))

    # ------------------------------------------------------------------
    # Sports — major finals
    # ------------------------------------------------------------------
    events.append(CalendarEvent(
        name="NBA Finals 2026 Game 7 (est.)",
        event_time=_unix_hhmm("2026-06-21", 23, 30),  # ~7:30pm ET tip-off
        keywords=["nba", "finals", "basketball", "championship", "nba finals"],
        size_multiplier=1.6,
        category="sports",
        window_before_hours=2.0,
        window_after_hours=1.0,
    ))

    events.append(CalendarEvent(
        name="FIFA World Cup 2026 Final",
        event_time=_unix_hhmm("2026-07-19", 20, 0),
        keywords=["world cup", "fifa", "football", "soccer", "final",
                  "world cup final", "champions"],
        size_multiplier=2.0,
        category="sports",
        window_before_hours=3.0,
        window_after_hours=2.0,
    ))

    events.append(CalendarEvent(
        name="Super Bowl LXI 2026",
        event_time=_unix_hhmm("2026-02-08", 23, 30),
        keywords=["super bowl", "nfl", "football", "super bowl lxi",
                  "championship", "nfl championship"],
        size_multiplier=1.8,
        category="sports",
        window_before_hours=3.0,
        window_after_hours=2.0,
    ))

    # ------------------------------------------------------------------
    # US GDP releases (advance estimate) — typically 12:30 UTC (8:30am ET)
    # ------------------------------------------------------------------
    gdp_dates = [
        ("2026-04-29", 12, 30),  # Q1 2026 advance
        ("2026-07-29", 12, 30),  # Q2 2026 advance
    ]
    for date_str, h, m in gdp_dates:
        events.append(CalendarEvent(
            name=f"US GDP Advance Estimate {date_str}",
            event_time=_unix_hhmm(date_str, h, m),
            keywords=["gdp", "gross domestic product", "economic growth", "recession",
                      "q1 gdp", "q2 gdp", "us economy"],
            size_multiplier=1.6,
            category="macro",
            window_before_hours=1.0,
            window_after_hours=1.5,
        ))

    # ------------------------------------------------------------------
    # FOMC meeting minutes releases — 3 weeks after each meeting, 18:00 UTC
    # ------------------------------------------------------------------
    fomc_minutes = [
        ("2026-04-08", 18, 0),  # Jan/Feb meeting minutes
        ("2026-05-27", 18, 0),  # Mar meeting minutes
    ]
    for date_str, h, m in fomc_minutes:
        events.append(CalendarEvent(
            name=f"FOMC Minutes Release {date_str}",
            event_time=_unix_hhmm(date_str, h, m),
            keywords=["fomc minutes", "fed minutes", "federal reserve", "monetary policy",
                      "interest rate", "rate path", "powell"],
            size_multiplier=1.4,
            category="macro",
            window_before_hours=0.5,
            window_after_hours=1.0,
        ))

    # ------------------------------------------------------------------
    # Major crypto events 2026
    # ------------------------------------------------------------------
    events.append(CalendarEvent(
        name="Ethereum ETF Options Launch (est.)",
        event_time=_unix_hhmm("2026-04-14", 14, 0),
        keywords=["ethereum", "eth", "etf", "options", "crypto", "ether"],
        size_multiplier=1.5,
        category="crypto",
        window_before_hours=4.0,
        window_after_hours=2.0,
    ))

    events.append(CalendarEvent(
        name="Bitcoin Monthly Options Expiry (April)",
        event_time=_unix_hhmm("2026-04-24", 8, 0),  # Last Friday, 8am UTC = Deribit expiry
        keywords=["bitcoin", "btc", "options expiry", "options", "deribit", "crypto"],
        size_multiplier=1.4,
        category="crypto",
        window_before_hours=2.0,
        window_after_hours=1.0,
    ))

    events.append(CalendarEvent(
        name="Bitcoin Monthly Options Expiry (May)",
        event_time=_unix_hhmm("2026-05-29", 8, 0),
        keywords=["bitcoin", "btc", "options expiry", "options", "deribit", "crypto"],
        size_multiplier=1.4,
        category="crypto",
        window_before_hours=2.0,
        window_after_hours=1.0,
    ))

    events.append(CalendarEvent(
        name="Bitcoin Monthly Options Expiry (June)",
        event_time=_unix_hhmm("2026-06-26", 8, 0),
        keywords=["bitcoin", "btc", "options expiry", "options", "deribit", "crypto"],
        size_multiplier=1.4,
        category="crypto",
        window_before_hours=2.0,
        window_after_hours=1.0,
    ))

    # ------------------------------------------------------------------
    # Major earnings (market-moving for prediction market activity)
    # ------------------------------------------------------------------
    earnings_events = [
        ("2026-04-23", "Meta Q1 Earnings", ["meta", "facebook", "zuckerberg", "ai", "metaverse"], 1.3),
        ("2026-04-24", "Alphabet Q1 Earnings", ["google", "alphabet", "ai", "search", "cloud"], 1.3),
        ("2026-04-30", "Microsoft Q3 Earnings", ["microsoft", "msft", "ai", "azure", "openai"], 1.3),
        ("2026-05-01", "Apple Q2 Earnings", ["apple", "aapl", "iphone", "ai", "tim cook"], 1.3),
        ("2026-05-07", "Amazon Q1 Earnings", ["amazon", "aws", "ai", "cloud", "ecommerce"], 1.3),
        ("2026-07-22", "Tesla Q2 Earnings", ["tesla", "elon musk", "ev", "electric vehicle", "musk"], 1.3),
    ]
    for date_str, name, keywords, mult in earnings_events:
        events.append(CalendarEvent(
            name=name,
            event_time=_unix_hhmm(date_str, 20, 30),  # ~4:30pm ET after-hours
            keywords=keywords,
            size_multiplier=mult,
            category="macro",
            window_before_hours=1.0,
            window_after_hours=2.0,
        ))

    # ------------------------------------------------------------------
    # Sports — Stanley Cup Finals 2026
    # ------------------------------------------------------------------
    events.append(CalendarEvent(
        name="Stanley Cup Finals 2026 (est. Game 7)",
        event_time=_unix_hhmm("2026-06-15", 23, 0),
        keywords=["stanley cup", "nhl", "hockey", "nhl finals", "ice hockey"],
        size_multiplier=1.5,
        category="sports",
        window_before_hours=2.0,
        window_after_hours=1.0,
    ))

    # ------------------------------------------------------------------
    # Sports — Wimbledon 2026 Finals
    # ------------------------------------------------------------------
    events.append(CalendarEvent(
        name="Wimbledon 2026 Men's Final",
        event_time=_unix_hhmm("2026-07-12", 13, 0),  # ~1pm UTC
        keywords=["wimbledon", "tennis", "grand slam", "djokovic", "alcaraz", "sinner"],
        size_multiplier=1.3,
        category="sports",
        window_before_hours=1.0,
        window_after_hours=1.0,
    ))

    # ------------------------------------------------------------------
    # Generic "always-on" event: US market hours (Mon-Fri 9:30am-4pm ET)
    # This is evaluated dynamically by checking current weekday + time.
    # We represent it as a recurring sentinel with multiplier 1.0.
    # The EventDrivenStrategy.scan() has special handling for this.
    # We use a special event_time=0 as a sentinel for "check in scan()".
    # ------------------------------------------------------------------
    events.append(CalendarEvent(
        name="US Market Hours (always-on)",
        event_time=0.0,         # sentinel — handled specially in scan()
        keywords=[],            # matches all markets
        size_multiplier=1.0,
        category="macro",
        window_before_hours=0.0,
        window_after_hours=0.0,
    ))

    return events


def _load_custom_events(path: str = "logs/custom_events.json") -> list[CalendarEvent]:
    """Load user-defined events from a JSON file if it exists."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        events = []
        for item in raw:
            events.append(CalendarEvent(
                name=item["name"],
                event_time=float(item["event_time"]),
                keywords=list(item.get("keywords", [])),
                size_multiplier=float(item.get("size_multiplier", 1.0)),
                category=item.get("category", "custom"),
                window_before_hours=float(item.get("window_before_hours", 2.0)),
                window_after_hours=float(item.get("window_after_hours", 1.0)),
            ))
        logger.info(f"[EventDriven] Loaded {len(events)} custom event(s) from {path}")
        return events
    except Exception as exc:
        logger.warning(f"[EventDriven] Failed to load custom events from {path}: {exc}")
        return []


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class EventDrivenStrategy(BaseStrategy):
    """
    Event-driven arbitrage strategy.

    Monitors a calendar of major macro/crypto/sports/politics events.
    When an event window is active, the strategy scans Polymarket for
    related markets, amplifies position sizes via the event's size_multiplier,
    and relaxes the minimum edge threshold to capture high-conviction moves
    during volatile periods.
    """

    _US_MARKET_OPEN_UTC_HOUR = 13    # 9:30am ET = 13:30 UTC (EST) / 14:30 UTC (EDT)
    _US_MARKET_OPEN_UTC_MINUTE = 30
    _US_MARKET_CLOSE_UTC_HOUR = 20   # 4:00pm ET = 20:00 UTC (EST) / 21:00 UTC (EDT)
    _US_MARKET_CLOSE_UTC_MINUTE = 0

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        self._calendar: list[CalendarEvent] = (
            get_default_calendar() + _load_custom_events()
        )
        self._last_custom_load: float = time.time()
        self._custom_reload_interval: float = 300.0   # reload every 5 minutes
        # LLM ensemble cache: condition_id -> (probability, cached_at_timestamp)
        self._llm_cache: dict[str, tuple[float, float]] = {}
        self._llm_cache_ttl: float = 1800.0  # 30-minute cache per market

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_event(self, event: CalendarEvent) -> None:
        """Dynamically add an event to the in-memory calendar."""
        self._calendar.append(event)
        self.log(f"Added event: {event.name} @ {event.event_time} [{event.category}]")

    def get_active_events(self, now: float | None = None) -> list[CalendarEvent]:
        """Return all calendar events whose window covers `now`."""
        if now is None:
            now = time.time()
        active = []
        for ev in self._calendar:
            if ev.event_time == 0.0:
                # Sentinel: US market hours — check current wall clock
                if self._is_us_market_hours(now):
                    active.append(ev)
                continue
            window_start = ev.event_time - ev.window_before_hours * 3600
            window_end = ev.event_time + ev.window_after_hours * 3600
            if window_start <= now <= window_end:
                active.append(ev)
        return active

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        """
        Scan for event-driven arbitrage opportunities.

        For each active event:
          - Find Polymarket markets whose question contains event keywords
          - Compute per-market edge using available orderbook mid prices
          - Apply event size_multiplier and relaxed min_edge
          - Emit BUY signals for underpriced YES or NO tokens
        """
        # Periodically reload custom events
        now = time.time()
        if now - self._last_custom_load > self._custom_reload_interval:
            new_custom = _load_custom_events()
            # Replace existing custom events (keep default calendar intact)
            self._calendar = [e for e in self._calendar if e.category != "custom"] + new_custom
            self._last_custom_load = now

        active_events = self.get_active_events(now)
        if not active_events:
            return []

        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})

        # Compute the effective min_edge for this cycle.
        # During event windows we relax the threshold by 20% to catch
        # more opportunities amid high volatility.
        base_min_edge: float = getattr(
            self.config.strategies, "rebalancing_min_edge", 0.02
        )
        event_min_edge: float = base_min_edge * 0.8

        # Build a combined keyword->event map for efficient matching
        # (a market may match multiple events; take the max multiplier)
        keyword_to_events: dict[str, list[CalendarEvent]] = {}
        for ev in active_events:
            if not ev.keywords:
                # No keywords = "always-on" event, matches all markets
                keyword_to_events.setdefault("__all__", []).append(ev)
                continue
            for kw in ev.keywords:
                keyword_to_events.setdefault(kw.lower(), []).append(ev)

        signals: list[Signal] = []

        for market in markets:
            if not market.active or market.closed:
                continue

            matched_events = self._match_market_to_events(
                market, keyword_to_events
            )
            if not matched_events:
                continue

            # Take the event with the highest size multiplier for this market
            best_event = max(matched_events, key=lambda e: e.size_multiplier)

            market_signals = self._generate_signals(
                market=market,
                orderbooks=orderbooks,
                event=best_event,
                min_edge=event_min_edge,
            )
            signals.extend(market_signals)

        if signals:
            event_names = ", ".join(e.name for e in active_events[:3])
            self.log(
                f"{len(signals)} signal(s) from {len(active_events)} active event(s): "
                f"{event_names}"
            )

        # LLM ensemble: run Claude probability estimates on top event-related markets
        llm_signals = await self._llm_scan(markets, orderbooks, active_events)
        signals.extend(llm_signals)

        return signals

    # ------------------------------------------------------------------
    # LLM Ensemble (Claude probability estimation)
    # ------------------------------------------------------------------

    async def _llm_scan(
        self,
        markets: list[Market],
        orderbooks: dict[str, Any],
        active_events: list[CalendarEvent],
    ) -> list[Signal]:
        """
        Run Claude claude-haiku-4-5-20251001 probability estimates on the most relevant event-related
        markets. Generates BUY signals when Claude's estimate diverges from the
        market price by more than ensemble_min_edge (default 5%).

        Key design choices (based on research findings):
        - Claude is NOT shown the current market price to avoid anchoring bias
        - Results are cached 30 minutes to limit API costs
        - Only evaluates top N markets per cycle (configurable)
        - Min liquidity filter ($2000 volume) to avoid illiquid markets
        """
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return []

        cfg = self.config.strategies
        ensemble_min_edge: float = getattr(cfg, "ensemble_min_edge", 0.05)
        max_markets: int = getattr(cfg, "ensemble_max_markets_per_cycle", 5)

        # Build keyword set from active events for fast pre-filter
        event_keywords: set[str] = set()
        event_context_map: dict[str, str] = {}  # keyword -> event name
        for ev in active_events:
            for kw in ev.keywords:
                event_keywords.add(kw.lower())
                event_context_map[kw.lower()] = ev.name

        if not event_keywords:
            return []

        # Select candidate markets: event-related, minimum liquidity, not near-resolved
        candidates: list[tuple[Market, str, float]] = []  # (market, event_context, yes_mid)
        for market in markets:
            if not market.active or market.closed:
                continue
            if market.volume < 2000:
                continue
            q_lower = market.question.lower()
            matched_kws = [kw for kw in event_keywords if kw in q_lower]
            if not matched_kws:
                continue

            yes_tok = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
            if not yes_tok:
                continue
            book = orderbooks.get(yes_tok.token_id)
            if not book or book.mid is None or book.best_ask is None:
                continue
            # Skip near-resolved markets — endgame sweep handles those
            if not (0.03 < book.mid < 0.97):
                continue

            ctx = event_context_map.get(matched_kws[0], "major market event")
            candidates.append((market, ctx, book.mid))

        if not candidates:
            return []

        # Sort by volume descending, take top N
        candidates.sort(key=lambda x: x[0].volume, reverse=True)
        candidates = candidates[:max_markets]

        # Run LLM estimates in parallel
        tasks = [
            self._llm_estimate_probability(market, event_ctx)
            for market, event_ctx, _ in candidates
        ]
        estimates = await asyncio.gather(*tasks, return_exceptions=True)

        signals: list[Signal] = []
        fee_cost = 2 * 0.002

        for (market, event_ctx, yes_mid), estimate in zip(candidates, estimates):
            if isinstance(estimate, Exception) or estimate is None:
                continue

            yes_tok = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
            no_tok = next((t for t in market.tokens if t.outcome.lower() == "no"), None)
            if not yes_tok:
                continue

            book = orderbooks.get(yes_tok.token_id)
            if not book or book.best_ask is None:
                continue

            # YES signal: Claude thinks YES probability >> market price
            yes_edge = estimate - book.best_ask - fee_cost
            if yes_edge >= ensemble_min_edge:
                size_usdc = self.risk.size_position(edge=yes_edge)
                if size_usdc >= 1.0:
                    self.log(
                        f"[LLM ENSEMBLE] BUY YES | {market.question[:55]} | "
                        f"Claude={estimate:.3f} vs market={book.best_ask:.3f} | "
                        f"edge={yes_edge:.3f} | size=${size_usdc:.2f}"
                    )
                    signals.append(Signal(
                        strategy="event_driven",
                        token_id=yes_tok.token_id,
                        side="BUY",
                        price=book.best_ask,
                        size_usdc=size_usdc,
                        edge=yes_edge,
                        notes=f"[LLM] YES | Claude={estimate:.3f} vs mkt={book.best_ask:.3f}",
                        metadata={
                            "source": "llm_ensemble",
                            "llm_probability": estimate,
                            "market_price": book.best_ask,
                            "event_context": event_ctx,
                        },
                    ))
                    continue

            # NO signal: Claude thinks YES probability << market price
            if no_tok:
                no_book = orderbooks.get(no_tok.token_id)
                if no_book and no_book.best_ask is not None:
                    no_fair_value = 1.0 - estimate
                    no_edge = no_fair_value - no_book.best_ask - fee_cost
                    if no_edge >= ensemble_min_edge:
                        size_usdc = self.risk.size_position(edge=no_edge)
                        if size_usdc >= 1.0:
                            self.log(
                                f"[LLM ENSEMBLE] BUY NO | {market.question[:55]} | "
                                f"Claude YES={estimate:.3f} → NO fair={no_fair_value:.3f} "
                                f"vs market NO={no_book.best_ask:.3f} | edge={no_edge:.3f}"
                            )
                            signals.append(Signal(
                                strategy="event_driven",
                                token_id=no_tok.token_id,
                                side="BUY",
                                price=no_book.best_ask,
                                size_usdc=size_usdc,
                                edge=no_edge,
                                notes=f"[LLM] NO | Claude YES={estimate:.3f} vs mkt NO={no_book.best_ask:.3f}",
                                metadata={
                                    "source": "llm_ensemble",
                                    "llm_yes_probability": estimate,
                                    "no_fair_value": no_fair_value,
                                    "event_context": event_ctx,
                                },
                            ))

        if signals:
            self.log(f"[LLM ENSEMBLE] {len(signals)} signal(s) from {len(candidates)} evaluated markets")

        return signals

    async def _llm_estimate_probability(
        self, market: Market, event_context: str
    ) -> float | None:
        """
        Ask Claude claude-haiku-4-5-20251001 for an independent probability estimate for a market.
        Returns a float 0–1, or None on failure.

        Critical: does NOT pass the current market price to Claude to avoid
        anchoring bias (documented failure mode in prediction market research).
        """
        cache_key = market.condition_id
        cached = self._llm_cache.get(cache_key)
        if cached is not None:
            prob, cached_at = cached
            if time.time() - cached_at < self._llm_cache_ttl:
                return prob

        try:
            import anthropic
            api_key = os.getenv("ANTHROPIC_API_KEY")
            client = anthropic.AsyncAnthropic(api_key=api_key)

            prompt = (
                f"You are an expert forecaster for prediction markets. "
                f"Estimate the probability that the following question resolves YES.\n\n"
                f"Question: {market.question}\n\n"
                f"Context: This is related to {event_context}. "
                f"Today's date is approximately {datetime.now(timezone.utc).strftime('%Y-%m-%d')}.\n\n"
                f"IMPORTANT: Give your independent probability estimate based on your knowledge. "
                f"Do NOT consider any market prices.\n\n"
                f"Respond with ONLY a JSON object, no other text:\n"
                f'{{\"probability\": 0.XX, \"confidence\": \"low|medium|high\", '
                f'\"reasoning\": \"one sentence\"}}\n\n'
                f"Probability must be between 0.0 and 1.0."
            )

            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            data = json.loads(text)
            prob = float(data["probability"])
            confidence = data.get("confidence", "medium")

            # Discount low-confidence estimates toward 0.5 (Bayesian shrinkage)
            if confidence == "low":
                prob = 0.5 + (prob - 0.5) * 0.5

            if 0.0 <= prob <= 1.0:
                self._llm_cache[cache_key] = (prob, time.time())
                logger.debug(
                    f"[LLM] {market.question[:50]} → {prob:.3f} "
                    f"(conf={confidence})"
                )
                return prob

        except json.JSONDecodeError as exc:
            logger.debug(f"[LLM] JSON parse error: {exc}")
        except Exception as exc:
            logger.debug(f"[LLM] Estimate failed for {market.question[:40]}: {exc}")

        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_us_market_hours(self, now: float) -> bool:
        """
        Return True if `now` falls within approximate US equity market hours
        (Mon-Fri, 13:30-20:00 UTC, approximating ET without DST adjustment).
        """
        dt = datetime.fromtimestamp(now, tz=timezone.utc)
        if dt.weekday() >= 5:   # Saturday=5, Sunday=6
            return False
        open_minutes = self._US_MARKET_OPEN_UTC_HOUR * 60 + self._US_MARKET_OPEN_UTC_MINUTE
        close_minutes = self._US_MARKET_CLOSE_UTC_HOUR * 60 + self._US_MARKET_CLOSE_UTC_MINUTE
        current_minutes = dt.hour * 60 + dt.minute
        return open_minutes <= current_minutes < close_minutes

    def _match_market_to_events(
        self,
        market: Market,
        keyword_to_events: dict[str, list[CalendarEvent]],
    ) -> list[CalendarEvent]:
        """
        Return the list of CalendarEvents that match this market's question.
        Always includes "always-on" (__all__) events.
        """
        question_lower = market.question.lower()
        matched: dict[str, CalendarEvent] = {}   # deduplicate by event name

        # Always-on events (no keyword filter)
        for ev in keyword_to_events.get("__all__", []):
            matched[ev.name] = ev

        # Keyword-matched events
        for kw, ev_list in keyword_to_events.items():
            if kw == "__all__":
                continue
            if kw in question_lower:
                for ev in ev_list:
                    matched[ev.name] = ev

        return list(matched.values())

    def _generate_signals(
        self,
        market: Market,
        orderbooks: dict[str, Orderbook],
        event: CalendarEvent,
        min_edge: float,
    ) -> list[Signal]:
        """
        For a given market and its best matching event, check YES and NO
        tokens for mis-pricing and return buy signals.

        The edge here is defined as the gap between fair value (0.5 default
        if no fair-value source is available) and the ask price.  In practice,
        the latency arb strategy handles precise fair-value computation;
        this strategy adds event-time size amplification on top.
        """
        if len(market.tokens) < 2:
            return []

        yes_token = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
        no_token = next((t for t in market.tokens if t.outcome.lower() == "no"), None)
        if not yes_token or not no_token:
            return []

        yes_book = orderbooks.get(yes_token.token_id)
        no_book = orderbooks.get(no_token.token_id)
        if not yes_book or not no_book:
            return []

        signals: list[Signal] = []
        fee_cost = 2 * 0.002   # estimated round-trip fee

        # ------------------------------------------------------------------
        # Use the mid prices. If YES + NO mids deviate from 1.0, we can
        # exploit the discrepancy.  Apply event multiplier to size.
        # ------------------------------------------------------------------
        yes_mid = yes_book.mid
        no_mid = no_book.mid
        if yes_mid is None or no_mid is None:
            return []

        mid_sum = yes_mid + no_mid

        # --- Case: sum of asks < 1 (buy both) ---
        if yes_book.best_ask and no_book.best_ask:
            ask_sum = yes_book.best_ask + no_book.best_ask
            gross_edge = 1.0 - ask_sum
            net_edge = gross_edge - fee_cost

            if net_edge >= min_edge:
                base_size = getattr(
                    self.config.strategies, "rebalancing_max_spend",
                    50.0
                )
                size_usdc = self.risk.size_position(edge=net_edge, base_size=base_size)
                size_usdc *= event.size_multiplier

                if size_usdc >= 1.0:
                    yes_contracts = size_usdc / yes_book.best_ask
                    self.log(
                        f"EVENT BUY BOTH | {event.name} | {market.question[:55]} | "
                        f"YES@{yes_book.best_ask:.3f} + NO@{no_book.best_ask:.3f} = {ask_sum:.3f} | "
                        f"edge={net_edge:.3f} | size=${size_usdc:.2f} | "
                        f"multiplier={event.size_multiplier}x"
                    )
                    signals.append(Signal(
                        strategy="event_driven",
                        token_id=yes_token.token_id,
                        side="BUY",
                        price=yes_book.best_ask,
                        size_usdc=size_usdc,
                        edge=net_edge,
                        notes=f"Event: {event.name} | cat={event.category}",
                        metadata={
                            "event_name": event.name,
                            "event_category": event.category,
                            "size_multiplier": event.size_multiplier,
                            "ask_sum": ask_sum,
                            "pair_token_id": no_token.token_id,
                        },
                    ))
                    signals.append(Signal(
                        strategy="event_driven",
                        token_id=no_token.token_id,
                        side="BUY",
                        price=no_book.best_ask,
                        size_usdc=yes_contracts * no_book.best_ask,
                        edge=net_edge,
                        notes=f"Event: {event.name} | cat={event.category} | NO leg",
                        metadata={
                            "event_name": event.name,
                            "event_category": event.category,
                            "size_multiplier": event.size_multiplier,
                            "ask_sum": ask_sum,
                            "pair_token_id": yes_token.token_id,
                        },
                    ))

        # --- Case: YES ask well below mid (YES underpriced) ---
        # Only fire if the mid_sum is close to 1.0 (sane market) but YES
        # ask is below mid by more than min_edge.
        elif yes_book.best_ask and abs(mid_sum - 1.0) < 0.05:
            deviation = yes_mid - yes_book.best_ask
            if deviation - fee_cost >= min_edge:
                base_size = getattr(
                    self.config.strategies, "rebalancing_max_spend", 50.0
                )
                size_usdc = self.risk.size_position(
                    edge=deviation - fee_cost, base_size=base_size
                ) * event.size_multiplier

                if size_usdc >= 1.0:
                    self.log(
                        f"EVENT BUY YES | {event.name} | {market.question[:55]} | "
                        f"YES ask={yes_book.best_ask:.3f} mid={yes_mid:.3f} | "
                        f"edge={deviation - fee_cost:.3f} | size=${size_usdc:.2f} | "
                        f"multiplier={event.size_multiplier}x"
                    )
                    signals.append(Signal(
                        strategy="event_driven",
                        token_id=yes_token.token_id,
                        side="BUY",
                        price=yes_book.best_ask,
                        size_usdc=size_usdc,
                        edge=deviation - fee_cost,
                        notes=f"Event: {event.name} | cat={event.category} | YES underpriced",
                        metadata={
                            "event_name": event.name,
                            "event_category": event.category,
                            "size_multiplier": event.size_multiplier,
                        },
                    ))

        return signals
