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
        "The bot currently implements: YES/NO rebalancing arb, combinatorial arb, "
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
        '  ]\n'
        "}\n\n"
        "Limit findings to the 8 most impactful. Be concrete, not generic."
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
    return findings_data
