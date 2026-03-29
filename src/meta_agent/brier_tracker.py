"""
Brier score tracker for the meta-agent.

Brier score = mean((predicted_prob - outcome)^2)
Lower is better. Ranges: 0.25 = random baseline, 0.18 = meaningful edge,
sub-0.12 = professional calibration.

This module:
1. Reads the portfolio trade log (portfolio_state.json or trade_log.jsonl)
2. Computes per-strategy Brier scores from resolved trades
3. Exposes compute_brier_scores() for analyzer.py to call
4. Provides log_trade_result() for recording resolved trades with probabilities

Integration: called from PortfolioSnapshot.to_analysis_dict() so the
meta-agent Claude prompt automatically includes calibration data.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

TRADE_LOG_PATH = Path(os.getenv("TRADE_LOG_PATH", "logs/trade_brier_log.jsonl"))


def log_trade_result(
    strategy: str,
    predicted_prob: float,
    outcome: float,          # 1.0 = won, 0.0 = lost
    net_pnl: float,
    arb_type: str = "",      # "dual_side" | "snipe" | "oracle_lag" | "latency_arb" | ...
) -> None:
    """
    Append a resolved trade to the Brier log.
    Call this from auto_close_resolved_loop after a position closes.
    """
    record = {
        "ts": time.time(),
        "strategy": strategy,
        "arb_type": arb_type,
        "predicted_prob": predicted_prob,
        "outcome": outcome,
        "net_pnl": net_pnl,
    }
    TRADE_LOG_PATH.parent.mkdir(exist_ok=True)
    with open(TRADE_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def compute_brier_scores(
    max_age_days: int = 60,
    min_trades: int = 5,
) -> dict:
    """
    Compute per-strategy and overall Brier scores from the trade log.
    Returns a dict with:
        {
            "overall": float,
            "by_strategy": {strategy_name: {"brier": float, "n": int, "grade": str}},
            "total_trades": int,
            "random_baseline": 0.25,
        }
    Returns empty/baseline values if insufficient data.
    """
    result = {
        "overall": 0.25,
        "by_strategy": {},
        "total_trades": 0,
        "random_baseline": 0.25,
        "note": "",
    }

    if not TRADE_LOG_PATH.exists():
        result["note"] = "No Brier log yet — will populate as positions resolve"
        return result

    cutoff = time.time() - max_age_days * 86400
    trades: list[dict] = []

    try:
        with open(TRADE_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("ts", 0) >= cutoff and rec.get("outcome") is not None:
                        trades.append(rec)
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        result["note"] = f"Log read error: {exc}"
        return result

    result["total_trades"] = len(trades)

    if len(trades) < min_trades:
        result["note"] = f"Only {len(trades)} resolved trades (need {min_trades})"
        return result

    # Overall Brier
    overall = sum(
        (t["predicted_prob"] - t["outcome"]) ** 2 for t in trades
    ) / len(trades)
    result["overall"] = round(overall, 4)

    # Per-strategy
    by_strat: dict[str, list] = {}
    for t in trades:
        s = t.get("strategy", "unknown")
        by_strat.setdefault(s, []).append(t)

    for strat, strat_trades in by_strat.items():
        if len(strat_trades) < min_trades:
            continue
        brier = sum(
            (t["predicted_prob"] - t["outcome"]) ** 2 for t in strat_trades
        ) / len(strat_trades)
        grade = (
            "professional" if brier < 0.12 else
            "edge" if brier < 0.18 else
            "marginal" if brier < 0.22 else
            "no_edge"
        )
        result["by_strategy"][strat] = {
            "brier": round(brier, 4),
            "n": len(strat_trades),
            "grade": grade,
        }

    return result
