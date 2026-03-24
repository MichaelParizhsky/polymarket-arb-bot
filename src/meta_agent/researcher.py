"""
Hourly strategy research agent.
Searches the web for new prediction market strategies, tooling improvements,
and market intelligence. Writes findings to logs/research_YYYY-MM-DD_HH.json.
Read-only — suggestions only, never modifies bot code.
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime

from src.utils.logger import logger

# Default interval in hours (override with RESEARCH_INTERVAL_HOURS env var)
DEFAULT_INTERVAL_HOURS = 2

# Searches per run (each costs ~$0.01 via Anthropic web search)
MAX_SEARCHES_PER_RUN = 5

# Rotating topic pool — indexed by run count so each run covers different ground
_TOPIC_POOL = [
    "polymarket prediction market arbitrage strategies 2025",
    "kalshi polymarket cross-exchange trading bot python",
    "prediction market market making liquidity strategy",
    "polymarket orderbook analysis edge finding techniques",
    "prediction market resolution arbitrage mispricing",
    "event-driven prediction market trading news catalyst",
    "kelly criterion optimal bet sizing prediction markets",
    "python asyncio high-frequency trading bot best practices",
    "prediction market automated trading academic research",
    "polymarket trading bot open source github strategies",
    "binance perpetual futures prediction market hedge",
    "combinatorial prediction market portfolio arbitrage",
]


def _pick_topics(run_index: int, count: int = 3) -> list[str]:
    """Pick `count` topics from the pool, rotating by run index."""
    offset = (run_index * count) % len(_TOPIC_POOL)
    return [_TOPIC_POOL[(offset + i) % len(_TOPIC_POOL)] for i in range(count)]


def _run_index() -> int:
    """Derive a monotonic run index from existing log files."""
    files = glob.glob("logs/research_*.json")
    return len(files)


async def run_research() -> dict:
    """
    Execute one research cycle via Claude with web search.
    Returns structured findings dict.
    Writes to logs/research_YYYY-MM-DD_HH.json.
    """
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.info("Research agent: skipped (no ANTHROPIC_API_KEY)")
        return {}

    run_idx = _run_index()
    topics = _pick_topics(run_idx)
    now = time.time()
    dt = datetime.fromtimestamp(now)

    logger.info(f"Research agent: run #{run_idx} — searching {len(topics)} topics...")

    system = (
        "You are a research agent for a live Polymarket/Kalshi prediction market "
        "arbitrage bot. Your job is to search the web for new strategies, improvements, "
        "and market intelligence that could make the bot more profitable or robust.\n\n"
        "The bot currently implements: combinatorial arb, "
        "Binance latency arb, passive market making, resolution arb (near-expiry), "
        "event-driven directional trading, cross-exchange Poly↔Kalshi arb.\n\n"
        "For each topic you search, extract the most novel and actionable findings. "
        "Ignore anything already implemented. Prioritize concrete techniques with "
        "evidence of profitability.\n\n"
        "Respond with ONLY valid JSON (no markdown fences):\n"
        "{\n"
        '  "findings": [\n'
        '    {\n'
        '      "title": "<concise title>",\n'
        '      "source": "<domain or URL>",\n'
        '      "relevance": <"high"|"medium"|"low">,\n'
        '      "category": <"strategy"|"tooling"|"risk"|"market_intel"|"research">,\n'
        '      "summary": "<what was found and why it matters>",\n'
        '      "actionable_suggestion": "<specific change to implement in the bot>"\n'
        '    }\n'
        '  ],\n'
        '  "top_insights": ["<key insight 1>", "<key insight 2>", ...],\n'
        '  "suggested_experiments": [\n'
        '    "<specific backtest or code change to try>"\n'
        '  ],\n'
        '  "signals": {\n'
        '    "active_topics": ["<keyword1>", "<keyword2>", ...],\n'
        '    "strategy_focus": "<strategy_name or null>",\n'
        '    "param_hints": {\n'
        '      "<ENV_KEY>": "<conservative_value>"\n'
        '    },\n'
        '    "confidence": "<high|medium|low>"\n'
        '  }\n'
        "}\n\n"
        "Limit findings to the 8 most impactful. Be concrete, not generic.\n"
        "For signals.active_topics: list 5-15 specific keyword phrases that are HOT right now in prediction markets (e.g. \"trump tariffs\", \"fed rate cut june\", \"ethereum etf staking\"). These are injected directly into the combinatorial strategy's topic scanner.\n"
        "For signals.param_hints: ONLY include if a finding strongly justifies a parameter nudge. Valid keys: latency_price_lag_threshold (float 0.005-0.05), combo_min_edge (float 0.01-0.10). Keep nudges conservative (< 20% change from defaults).\n"
        "For signals.strategy_focus: name of the strategy most favored by this run's findings, or null."
    )

    user_msg = (
        "Search the web for the following prediction market trading topics "
        "and synthesize the most actionable findings for our arbitrage bot:\n\n"
        + "\n".join(f"- {t}" for t in topics)
        + "\n\nFocus on novel techniques, recent research, or open-source "
        "implementations not yet covered by our current strategies."
    )

    client = anthropic.AsyncAnthropic(api_key=api_key)
    web_search_available = True

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            tools=[{"type": "web_search_20260209", "max_uses": MAX_SEARCHES_PER_RUN}],
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        logger.warning(f"Research agent: web search unavailable ({exc}), using training knowledge")
        web_search_available = False
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system,
            messages=[{
                "role": "user",
                "content": user_msg + "\n\n(Web search unavailable — draw from training knowledge only.)",
            }],
        )

    raw = next((b.text for b in response.content if hasattr(b, "text")), "")

    findings_data: dict = {}
    try:
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```", 2)[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.rsplit("```", 1)[0]
        findings_data = json.loads(clean.strip())
    except json.JSONDecodeError as exc:
        logger.warning(f"Research agent: JSON parse failed: {exc}")
        findings_data = {
            "findings": [],
            "top_insights": [],
            "suggested_experiments": [],
            "parse_error": True,
            "raw_response": raw[:3000],
        }

    findings = findings_data.get("findings", [])
    findings_data.update({
        "timestamp": now,
        "date": dt.strftime("%Y-%m-%d"),
        "run_hour": dt.strftime("%H:00"),
        "run_index": run_idx,
        "topics_searched": topics,
        "web_search_used": web_search_available,
        "finding_count": len(findings),
        "high_count": sum(1 for f in findings if f.get("relevance") == "high"),
        "medium_count": sum(1 for f in findings if f.get("relevance") == "medium"),
    })

    # Write structured signals for strategies to consume
    signals_payload = findings_data.get("signals", {})
    if signals_payload:
        signals_payload["timestamp"] = now
        signals_payload["source_run"] = dt.strftime("%Y-%m-%d_%H")
        try:
            with open("logs/research_signals.json", "w") as f:
                json.dump(signals_payload, f, indent=2)
        except Exception as exc:
            logger.warning(f"Research agent: could not write signals: {exc}")

    os.makedirs("logs", exist_ok=True)
    out_path = f"logs/research_{dt.strftime('%Y-%m-%d_%H')}.json"
    with open(out_path, "w") as f:
        json.dump(findings_data, f, indent=2)

    # Keep only last 48 research files
    old_files = sorted(glob.glob("logs/research_*.json"), reverse=True)[48:]
    for old in old_files:
        try:
            os.remove(old)
        except OSError:
            pass

    logger.info(
        f"Research agent: {len(findings)} findings "
        f"({findings_data['high_count']} high, {findings_data['medium_count']} medium) "
        f"| web_search={'yes' if web_search_available else 'no'}"
    )

    # Generate a strategy proposal from the top strategy finding (if any)
    strategy_findings = [
        f for f in findings
        if f.get("category") == "strategy" and f.get("relevance") == "high"
    ]
    if strategy_findings:
        try:
            await generate_strategy_proposal(strategy_findings[0])
        except Exception as exc:
            logger.warning(f"Research agent: proposal generation failed: {exc}")

    return findings_data


async def generate_strategy_proposal(finding: dict) -> dict | None:
    """
    Given a high-relevance strategy finding, ask Claude to write a complete
    Python strategy module following BaseStrategy interface.
    Returns metadata dict with keys: name, file_path, code, finding, timestamp.
    Writes files to logs/proposals/.
    """
    import anthropic as _anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    base_strategy_src = '''from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from src.utils.logger import logger

@dataclass
class Signal:
    strategy: str
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size_usdc: float
    edge: float
    notes: str = ""
    metadata: dict = field(default_factory=dict)

class BaseStrategy(ABC):
    def __init__(self, config, portfolio, risk_manager) -> None:
        self.config = config
        self.portfolio = portfolio
        self.risk = risk_manager
        self.name = self.__class__.__name__

    @abstractmethod
    async def scan(self, context: dict[str, Any]) -> list[Signal]: ...

    def log(self, msg: str, level: str = "info") -> None:
        getattr(logger, level)(f"[{self.name}] {msg}")
'''

    prompt = f"""You are writing a new Python trading strategy module for a Polymarket prediction market arbitrage bot.

## Research Finding to Implement
Title: {finding.get('title', '')}
Category: {finding.get('category', '')}
Summary: {finding.get('summary', '')}
Actionable Suggestion: {finding.get('actionable_suggestion', '')}

## BaseStrategy Interface (you MUST subclass this)
```python
{base_strategy_src}
```

## Context dict available in scan():
- context["markets"]: list of Market objects, each with .question (str), .tokens (list), .active (bool), .closed (bool), .volume (float), .end_date_iso (str)
- context["orderbooks"]: dict[token_id, Orderbook], Orderbook has .best_bid, .best_ask, .mid, .bids, .asks
- context["binance_feed"]: BinanceFeed with .get_price(symbol) -> PriceTick(.price), .is_stale(symbol, max_age_seconds)
- self.risk.size_position(edge: float) -> float  (returns USDC amount to risk)
- self.risk.check_trade(token_id, side, usdc_amount, strategy) -> (bool, str)
- self.portfolio.positions: dict[token_id, Position(.contracts, .cost_basis)]
- self.config.risk.min_edge_threshold, self.config.risk.max_position_size
- FEE_RATE = 0.002, SLIPPAGE_RATE = 0.002

## Requirements
1. Class name must be CamelCase ending in "Strategy"
2. Must import from `src.strategies.base import BaseStrategy, Signal`
3. scan() must be async and return list[Signal]
4. Only generate BUY signals (SELL signals require holding the position)
5. Always subtract 2*(FEE_RATE+SLIPPAGE_RATE) from gross edge
6. Only emit signals when net_edge >= self.config.risk.min_edge_threshold
7. Keep the module under 150 lines
8. No external dependencies beyond stdlib + what's already imported in the bot

Write ONLY the Python module code. No markdown fences, no explanation."""

    client = _anthropic.AsyncAnthropic(api_key=api_key)
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        code = response.content[0].text.strip()
        if code.startswith("```"):
            code = code.split("```")[1]
            if code.startswith("python"):
                code = code[6:]
            code = code.rsplit("```", 1)[0].strip()
    except Exception as exc:
        logger.warning(f"Strategy proposal generation failed: {exc}")
        return None

    # Validate syntax
    import ast as _ast
    try:
        _ast.parse(code)
    except SyntaxError as exc:
        logger.warning(f"Strategy proposal has syntax error: {exc}")
        return None

    # Extract class name
    import re as _re
    class_match = _re.search(r"class\s+(\w+Strategy)\b", code)
    if not class_match:
        logger.warning("Strategy proposal: no Strategy class found")
        return None

    class_name = class_match.group(1)
    ts = int(time.time())
    safe_name = _re.sub(r"[^a-z0-9_]", "_", class_name.lower())
    file_name = f"proposed_{safe_name}_{ts}.py"

    os.makedirs("logs/proposals", exist_ok=True)
    file_path = f"logs/proposals/{file_name}"
    meta_path = f"logs/proposals/{file_name[:-3]}_meta.json"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(code)

    meta = {
        "id": f"{safe_name}_{ts}",
        "class_name": class_name,
        "file_name": file_name,
        "file_path": file_path,
        "finding_title": finding.get("title", ""),
        "finding_summary": finding.get("summary", ""),
        "actionable_suggestion": finding.get("actionable_suggestion", ""),
        "timestamp": ts,
        "deployed": False,
        "deployed_at": None,
        "deployed_path": None,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    logger.info(f"Research agent: strategy proposal written → {file_path} ({class_name})")
    return meta
