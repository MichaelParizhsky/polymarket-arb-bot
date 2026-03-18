"""
Enhanced FastAPI dashboard with tabs for bot + meta-agent.
Visit http://localhost:5000
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import time

from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Optional API key for destructive endpoints (set DASHBOARD_API_KEY env var to enable)
_DASHBOARD_API_KEY: str = os.getenv("DASHBOARD_API_KEY", "")


def _check_api_key(x_api_key: str = Header(default="")) -> bool:
    """Return True if request is authorized. Always passes when no key is configured."""
    if not _DASHBOARD_API_KEY:
        return True
    return x_api_key == _DASHBOARD_API_KEY

_portfolio = None
_bot_start_time = time.time()
_cycle_count = 0
_config = None
_risk = None
_binance_ref = None
_kalshi_ref = None


def register(portfolio, start_time: float, config=None, risk=None, binance=None, kalshi=None) -> None:
    global _portfolio, _bot_start_time, _config, _risk, _binance_ref, _kalshi_ref
    _portfolio = portfolio
    _bot_start_time = start_time
    _config = config
    _risk = risk
    _binance_ref = binance
    _kalshi_ref = kalshi


@app.post("/api/reset")
def reset_portfolio(x_api_key: str = Header(default="")):
    if not _check_api_key(x_api_key):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    global _bot_start_time
    if not _portfolio:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=503)
    starting = _portfolio.starting_balance
    _portfolio.usdc_balance = starting
    _portfolio.positions.clear()
    _portfolio.trades.clear()
    _portfolio.open_orders.clear()
    _portfolio.closed_positions.clear()
    _portfolio._trade_counter = 0
    _portfolio.pnl_history = [{"t": time.time(), "value": starting, "pnl": 0.0}]
    _bot_start_time = time.time()
    _portfolio.save_to_json()
    return {"ok": True, "starting_balance": starting}


# ------------------------------------------------------------------ #
#  Bot API endpoints                                                   #
# ------------------------------------------------------------------ #

@app.get("/api/status")
def status():
    if not _portfolio:
        return {"status": "starting"}
    p = _portfolio
    uptime = int(time.time() - _bot_start_time)
    total_pnl = round(p.total_pnl(), 2)
    realized = round(p.realized_closed_pnl(), 2)
    trades_per_hour = round(len(p.trades) / max(uptime / 3600, 0.01), 1)
    return {
        "status": "running",
        "paper_trading": True,
        "uptime_seconds": uptime,
        "uptime": _fmt_uptime(uptime),
        "cycle_count": _cycle_count,
        "balance": round(p.usdc_balance, 2),
        "starting_balance": round(p.starting_balance, 2),
        "total_value": round(p.total_value(), 2),
        "pnl": total_pnl,
        "pnl_pct": round((total_pnl / p.starting_balance) * 100, 3),
        "realized_pnl": realized,
        "realized_pnl_pct": round((realized / p.starting_balance) * 100, 3),
        "open_positions": len(p.positions),
        "closed_positions": len(p.closed_positions),
        "total_trades": len(p.trades),
        "exposure": round(p.exposure(), 2),
        "fees_paid": round(p.total_fees_paid(), 2),
        "win_rate": p.win_rate(),
        "trades_per_hour": trades_per_hour,
    }


@app.get("/api/pnl_history")
def pnl_history():
    if not _portfolio:
        return []
    return _portfolio.pnl_history


@app.get("/api/positions")
def positions():
    if not _portfolio:
        return []
    return [
        {
            "token_id": tid[:16] + "...",
            "question": pos.market_question[:70],
            "outcome": pos.outcome,
            "contracts": round(pos.contracts, 4),
            "avg_cost": round(pos.avg_cost, 4),
            "cost_basis": round(pos.cost_basis, 2),
            "strategy": pos.strategy,
            "opened_at": int(pos.opened_at),
        }
        for tid, pos in _portfolio.positions.items()
    ]


@app.get("/api/closed_positions")
def closed_positions(limit: int = 100):
    if not _portfolio:
        return []
    recent = list(reversed(_portfolio.closed_positions))[:limit]
    return recent


@app.get("/api/trades")
def trades(limit: int = 100):
    if not _portfolio:
        return []
    recent = list(reversed(_portfolio.trades))[:limit]
    return [
        {
            "trade_id": t.trade_id,
            "strategy": t.strategy,
            "side": t.side,
            "contracts": round(t.contracts, 4),
            "price": round(t.price, 4),
            "usdc_amount": round(t.usdc_amount, 2),
            "fee": round(t.fee, 4),
            "timestamp": int(t.timestamp),
            "notes": t.notes[:80],
        }
        for t in recent
    ]


@app.get("/api/strategy_pnl")
def strategy_pnl():
    if not _portfolio:
        return {}
    return {k: round(v, 2) for k, v in _portfolio.strategy_pnl().items()}


@app.get("/api/strategy_trades")
def strategy_trades():
    """Trade counts per strategy over time buckets."""
    if not _portfolio:
        return {}
    counts: dict[str, int] = {}
    for t in _portfolio.trades:
        counts[t.strategy] = counts.get(t.strategy, 0) + 1
    return counts


@app.get("/api/logs")
def logs(since: float = 0, limit: int = 200):
    from src.utils.logger import get_log_buffer
    all_logs = get_log_buffer()
    filtered = [l for l in all_logs if l["t"] > since]
    return filtered[-limit:]


@app.get("/api/logs/stream")
async def logs_stream():
    """SSE stream of log lines."""
    from src.utils.logger import get_log_buffer
    async def generator():
        last_count = 0
        while True:
            buf = get_log_buffer()
            if len(buf) > last_count:
                for entry in buf[last_count:]:
                    yield {"data": json.dumps(entry)}
                last_count = len(buf)
            await asyncio.sleep(0.5)
    return EventSourceResponse(generator())


# ------------------------------------------------------------------ #
#  System & Analytics endpoints                                        #
# ------------------------------------------------------------------ #

@app.get("/api/system")
def system_status():
    """Return system connection status, strategy states, risk health, API keys, disk usage."""
    # --- Mode ---
    mode = "PAPER"
    if _config is not None:
        try:
            mode = "LIVE" if not _config.paper_trading else "PAPER"
        except Exception:
            pass

    # --- Strategies ---
    strategy_notes = {
        "rebalancing":    "Trades YES+NO deviation from $1",
        "combinatorial":  "Multi-outcome portfolio imbalance",
        "latency_arb":    "Polymarket lagging Binance prices",
        "market_making":  "Passive liquidity / earn spread",
        "resolution":     "Mispriced near-expiry markets",
        "event_driven":   "News/event catalyst markets",
        "cross_exchange": "Polymarket vs Kalshi divergence",
        "futures_hedge":  "Binance futures hedge on crypto",
    }
    strategies = {}
    if _config is not None:
        try:
            cfg_s = _config.strategies
            for name, note in strategy_notes.items():
                attr = f"{name}_enabled"
                enabled = bool(getattr(cfg_s, attr, False))
                if not enabled and name == "latency_arb":
                    note = "Disabled — dynamic fees killed edge"
                strategies[name] = {"enabled": enabled, "note": note}
        except Exception:
            pass
    if not strategies:
        for name, note in strategy_notes.items():
            strategies[name] = {"enabled": False, "note": note}

    # --- Connections ---
    # Polymarket: just check if _portfolio is registered (proxy for connectivity)
    poly_ok = _portfolio is not None
    connections = {
        "polymarket": {
            "status": "ok" if poly_ok else "warn",
            "detail": "REST + WS active" if poly_ok else "Not yet connected",
        },
        "binance": {"status": "error", "detail": "Not configured"},
        "kalshi": {"status": "error", "detail": "Not configured"},
    }
    if _binance_ref is not None:
        try:
            if callable(getattr(_binance_ref, "is_connected", None)):
                connected = _binance_ref.is_connected()
            else:
                connected = True
            connections["binance"] = {
                "status": "ok" if connected else "warn",
                "detail": "WebSocket connected" if connected else "Reconnecting...",
            }
        except Exception:
            connections["binance"] = {"status": "warn", "detail": "Status unknown"}
    if _kalshi_ref is not None:
        try:
            if callable(getattr(_kalshi_ref, "_has_credentials", None)):
                creds = _kalshi_ref._has_credentials()
            else:
                creds = True
            connections["kalshi"] = {
                "status": "ok" if creds else "warn",
                "detail": "Credentials loaded" if creds else "No credentials",
            }
        except Exception:
            connections["kalshi"] = {"status": "warn", "detail": "Status unknown"}

    # --- API keys (True/False only, never expose values) ---
    api_keys = {
        "anthropic":    bool(os.getenv("ANTHROPIC_API_KEY")),
        "polymarket":   bool(os.getenv("POLYMARKET_API_KEY") or os.getenv("POLY_API_KEY")),
        "kalshi_rsa":   bool(os.getenv("KALSHI_RSA_KEY") or os.getenv("KALSHI_PRIVATE_KEY")),
        "kalshi_token": bool(os.getenv("KALSHI_API_TOKEN") or os.getenv("KALSHI_TOKEN")),
    }

    # --- Risk health ---
    risk_data = {
        "health_score": None,
        "health_grade": "N/A",
        "hard_stop": False,
        "drawdown_pct": 0.0,
        "exposure_pct": 0.0,
        "flags": [],
    }
    if _risk is not None:
        try:
            h = _risk.portfolio_health_score()
            risk_data["health_score"] = h.get("score")
            risk_data["health_grade"] = h.get("grade", "N/A")
            risk_data["hard_stop"] = h.get("hard_stop", False)
            risk_data["drawdown_pct"] = h.get("drawdown_pct", 0.0)
            risk_data["exposure_pct"] = h.get("exposure_pct", 0.0)
            risk_data["flags"] = h.get("flags", [])
        except Exception:
            pass

    # --- Meta-agent info ---
    meta_agent = {"enabled": False, "interval_minutes": 30, "last_run_ago_minutes": None}
    meta_agent["enabled"] = bool(os.getenv("ANTHROPIC_API_KEY"))
    try:
        meta_agent["interval_minutes"] = int(os.getenv("META_AGENT_INTERVAL_MINUTES", "30"))
    except Exception:
        pass
    meta_files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)
    if meta_files:
        try:
            mtime = os.path.getmtime(meta_files[0])
            meta_agent["last_run_ago_minutes"] = round((time.time() - mtime) / 60, 1)
        except Exception:
            pass

    # --- Disk usage ---
    log_files = glob.glob("logs/*.log") + glob.glob("logs/meta_agent_*.json")
    total_bytes = 0
    for lf in log_files:
        try:
            total_bytes += os.path.getsize(lf)
        except OSError:
            pass
    disk = {
        "log_files_count": len(log_files),
        "log_files_mb": round(total_bytes / (1024 * 1024), 2),
    }

    return {
        "mode": mode,
        "strategies": strategies,
        "connections": connections,
        "api_keys": api_keys,
        "risk": risk_data,
        "meta_agent": meta_agent,
        "disk": disk,
    }


@app.get("/api/analytics")
def analytics():
    """Return strategy analytics, hourly PnL, health history, LLM decisions, edge distribution."""
    p = _portfolio

    # --- Strategy ROI, win rates, trade counts, volumes, fees ---
    strategy_roi: dict[str, float] = {}
    strategy_win_rates: dict[str, float] = {}
    strategy_trade_counts: dict[str, int] = {}
    strategy_volumes: dict[str, float] = {}
    strategy_fees: dict[str, float] = {}

    if p is not None:
        # Trade counts, volumes, fees from trades list
        vol_map: dict[str, float] = {}
        fee_map: dict[str, float] = {}
        count_map: dict[str, int] = {}
        for t in p.trades:
            s = t.strategy
            count_map[s] = count_map.get(s, 0) + 1
            vol_map[s] = vol_map.get(s, 0.0) + t.usdc_amount
            fee_map[s] = fee_map.get(s, 0.0) + t.fee
        strategy_trade_counts = count_map
        strategy_volumes = {k: round(v, 2) for k, v in vol_map.items()}
        strategy_fees = {k: round(v, 4) for k, v in fee_map.items()}

        # PnL per strategy
        strat_pnl = {}
        try:
            strat_pnl = p.strategy_pnl()
        except Exception:
            pass

        # ROI = pnl / volume
        for s, vol in vol_map.items():
            pnl_val = strat_pnl.get(s, 0.0)
            if vol > 0:
                strategy_roi[s] = round((pnl_val / vol) * 100, 3)
            else:
                strategy_roi[s] = 0.0

        # Win rates from closed_positions
        wins_map: dict[str, int] = {}
        total_map: dict[str, int] = {}
        for cp in p.closed_positions:
            s = getattr(cp, "strategy", None) or cp.get("strategy", "") if isinstance(cp, dict) else getattr(cp, "strategy", "")
            rp = cp.get("realized_pnl", 0) if isinstance(cp, dict) else getattr(cp, "realized_pnl", 0)
            total_map[s] = total_map.get(s, 0) + 1
            if rp > 0:
                wins_map[s] = wins_map.get(s, 0) + 1
        for s, total in total_map.items():
            strategy_win_rates[s] = round(wins_map.get(s, 0) / total * 100, 1) if total > 0 else 0.0

    # --- Hourly PnL (last 24h) ---
    hourly_pnl: list[dict] = []
    if p is not None and p.pnl_history:
        now = time.time()
        cutoff = now - 86400
        # bucket by hour
        buckets: dict[int, list[float]] = {}
        for point in p.pnl_history:
            t_val = point.get("t", 0)
            if t_val < cutoff:
                continue
            hour_bucket = int(t_val // 3600)
            buckets.setdefault(hour_bucket, []).append(point.get("pnl", 0.0))

        if buckets:
            sorted_hours = sorted(buckets.keys())
            prev_pnl = 0.0
            for hb in sorted_hours:
                last_pnl = buckets[hb][-1]
                delta = last_pnl - prev_pnl
                import datetime
                label = datetime.datetime.fromtimestamp(hb * 3600).strftime("%H:00")
                hourly_pnl.append({"hour_label": label, "pnl": round(delta, 4)})
                prev_pnl = last_pnl

    # --- Health history from meta_agent_*.json ---
    health_history: list[dict] = []
    meta_files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)[:20]
    for mf in reversed(meta_files):
        try:
            with open(mf) as fp:
                data = json.load(fp)
            h = data.get("health", {})
            score = h.get("score") or h.get("health_score")
            grade = h.get("grade") or h.get("health_grade", "?")
            ts_val = data.get("timestamp", 0)
            if score is not None:
                health_history.append({"t": ts_val, "score": score, "grade": grade})
        except Exception:
            pass

    # --- LLM decisions (trades with [LLM] in notes) ---
    llm_decisions: list[dict] = []
    if p is not None:
        for t in reversed(p.trades):
            notes = t.notes if isinstance(t.notes, str) else ""
            if "[LLM]" in notes:
                llm_decisions.append({
                    "trade_id": t.trade_id,
                    "strategy": t.strategy,
                    "side": t.side,
                    "price": round(t.price, 4),
                    "usdc_amount": round(t.usdc_amount, 2),
                    "timestamp": int(t.timestamp),
                    "notes": notes[:120],
                })
                if len(llm_decisions) >= 20:
                    break

    # --- Edge distribution (bucket edges seen in trade notes) ---
    edge_distribution: dict[str, int] = {
        "0-1%": 0, "1-2%": 0, "2-3%": 0, "3-5%": 0, "5-10%": 0, "10%+": 0
    }
    if p is not None:
        import re as _re
        for t in p.trades:
            notes = t.notes if isinstance(t.notes, str) else ""
            m = _re.search(r"edge[=:]\s*([\d.]+)", notes, _re.IGNORECASE)
            if m:
                try:
                    edge_pct = float(m.group(1)) * 100
                    if edge_pct < 1:
                        edge_distribution["0-1%"] += 1
                    elif edge_pct < 2:
                        edge_distribution["1-2%"] += 1
                    elif edge_pct < 3:
                        edge_distribution["2-3%"] += 1
                    elif edge_pct < 5:
                        edge_distribution["3-5%"] += 1
                    elif edge_pct < 10:
                        edge_distribution["5-10%"] += 1
                    else:
                        edge_distribution["10%+"] += 1
                except ValueError:
                    pass

    return {
        "strategy_roi": strategy_roi,
        "strategy_win_rates": strategy_win_rates,
        "strategy_trade_counts": strategy_trade_counts,
        "strategy_volumes": strategy_volumes,
        "strategy_fees": strategy_fees,
        "hourly_pnl": hourly_pnl,
        "health_history": health_history,
        "llm_decisions": llm_decisions,
        "edge_distribution": edge_distribution,
    }


# ------------------------------------------------------------------ #
#  Balances endpoint                                                   #
# ------------------------------------------------------------------ #

@app.get("/api/balances")
async def balances():
    """Estimated spend on Anthropic, Railway disk usage, billing cycle info."""
    import datetime

    now = time.time()
    today = datetime.date.today()

    # Billing cycle: 1st of this month → 1st of next month
    cycle_start = datetime.date(today.year, today.month, 1)
    if today.month == 12:
        cycle_end = datetime.date(today.year + 1, 1, 1)
    else:
        cycle_end = datetime.date(today.year, today.month + 1, 1)
    days_in_cycle = (cycle_end - cycle_start).days
    days_elapsed = max((today - cycle_start).days, 0)
    days_remaining = max((cycle_end - today).days, 0)
    cycle_pct = round(days_elapsed / days_in_cycle * 100, 1) if days_in_cycle else 0

    # --- Anthropic cost estimate ---
    # Each meta-agent run uses Claude Opus 4.6 with extended thinking.
    # ~2500 input tokens  @ $15/MTok = $0.0375
    # ~10000 output+think @ $75/MTok = $0.75
    # ≈ $0.79/run (conservative estimate)
    meta_files = sorted(glob.glob("logs/meta_agent_*.json"))
    meta_run_count = len(meta_files)
    COST_PER_RUN = 0.79
    estimated_anthropic_cost = round(meta_run_count * COST_PER_RUN, 2)
    daily_runs = meta_run_count / max(days_elapsed, 1)
    projected_monthly = round(daily_runs * days_in_cycle * COST_PER_RUN, 2)
    try:
        anthropic_budget: float | None = float(os.getenv("ANTHROPIC_MONTHLY_BUDGET") or 0) or None
    except Exception:
        anthropic_budget = None

    # --- Railway disk usage (local filesystem) ---
    log_files = (
        glob.glob("logs/*.log")
        + glob.glob("logs/meta_agent_*.json")
        + glob.glob("logs/*.json")
    )
    total_bytes = sum(
        os.path.getsize(f) for f in log_files if os.path.exists(f)
    )
    disk_used_mb = round(total_bytes / (1024 * 1024), 2)
    try:
        vol_limit_mb = int(os.getenv("RAILWAY_VOLUME_LIMIT_MB") or 512)
    except Exception:
        vol_limit_mb = 512
    disk_pct = round(disk_used_mb / vol_limit_mb * 100, 1) if vol_limit_mb else 0

    try:
        railway_base = float(os.getenv("RAILWAY_PLAN_COST") or 5)
    except Exception:
        railway_base = 5.0

    # --- Bot summary ---
    uptime_hours = round((now - _bot_start_time) / 3600, 2) if _bot_start_time else 0
    bot_trades = len(_portfolio.trades) if _portfolio else 0
    bot_pnl = round(_portfolio.total_pnl(), 2) if _portfolio else 0.0

    return {
        "billing_cycle": {
            "start": str(cycle_start),
            "end": str(cycle_end),
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "days_total": days_in_cycle,
            "cycle_pct": cycle_pct,
        },
        "anthropic": {
            "meta_agent_runs": meta_run_count,
            "cost_per_run_usd": COST_PER_RUN,
            "estimated_cost_usd": estimated_anthropic_cost,
            "projected_monthly_usd": projected_monthly,
            "monthly_budget_usd": anthropic_budget,
            "key_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
            "model": "claude-opus-4-6",
        },
        "railway": {
            "disk_used_mb": disk_used_mb,
            "disk_limit_mb": vol_limit_mb,
            "disk_pct": disk_pct,
            "plan_base_cost_usd": railway_base,
            "days_remaining_in_cycle": days_remaining,
        },
        "bot": {
            "uptime_hours": uptime_hours,
            "trades_executed": bot_trades,
            "total_pnl_usd": bot_pnl,
            "paper_trading": True,
        },
    }


# ------------------------------------------------------------------ #
#  Meta-agent API endpoints                                            #
# ------------------------------------------------------------------ #

@app.get("/api/meta/history")
def meta_history():
    files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)[:10]
    results = []
    for f in files:
        try:
            with open(f) as fp:
                data = json.load(fp)
            # Support both old format (portfolio_snapshot) and new format (portfolio_summary)
            old_snapshot = data.get("portfolio_snapshot", {})
            new_summary = data.get("portfolio_summary", {})
            portfolio_block = new_summary or old_snapshot.get("portfolio", {})
            portfolio_pnl = portfolio_block.get("total_pnl_usdc", 0)
            results.append({
                "file": os.path.basename(f),
                "timestamp": data.get("timestamp", 0),
                "proposed_changes": data.get("proposed_changes", {}),
                "applied_changes": data.get("applied_changes", []),
                "analysis_preview": data.get("analysis", "")[:300],
                "portfolio_pnl": portfolio_pnl,
                "health": data.get("health", {}),
                "strategy_roi_pct": data.get("strategy_roi_pct", {}),
            })
        except Exception:
            pass
    return results


@app.get("/api/meta/latest")
def meta_latest():
    files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)
    if not files:
        return {"found": False}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return {"found": True, **data}
    except Exception:
        return {"found": False}


# ------------------------------------------------------------------ #
#  Agent timers endpoint                                               #
# ------------------------------------------------------------------ #

@app.get("/api/agent_timers")
def agent_timers():
    """Return last-run timestamps + intervals so the dashboard can show countdowns."""
    now = time.time()

    # Meta-agent: runs every META_AGENT_INTERVAL_MINUTES (default 30)
    meta_interval = int(os.getenv("META_AGENT_INTERVAL_MINUTES", "30")) * 60
    meta_files = sorted(glob.glob("logs/meta_agent_[0-9]*.json"), reverse=True)
    meta_last = 0
    if meta_files:
        try:
            meta_last = os.path.getmtime(meta_files[0])
        except OSError:
            pass

    # Research: runs every RESEARCH_INTERVAL_HOURS (default 2)
    research_interval = float(os.getenv("RESEARCH_INTERVAL_HOURS", "2")) * 3600
    research_files = sorted(glob.glob("logs/research_*.json"), reverse=True)
    research_last = 0
    if research_files:
        try:
            research_last = os.path.getmtime(research_files[0])
        except OSError:
            pass

    # Code review: runs weekly (7 days)
    review_interval = 7 * 24 * 3600
    review_files = sorted(glob.glob("logs/code_review_*.json"), reverse=True)
    review_last = 0
    if review_files:
        try:
            review_last = os.path.getmtime(review_files[0])
        except OSError:
            pass

    def _next_in(last_ts, interval):
        if last_ts == 0:
            return None  # never run
        return max(0.0, last_ts + interval - now)

    return {
        "now": now,
        "meta_agent":    {"last_run": meta_last,     "interval_secs": meta_interval,     "next_in_secs": _next_in(meta_last, meta_interval)},
        "research":      {"last_run": research_last,  "interval_secs": research_interval,  "next_in_secs": _next_in(research_last, research_interval)},
        "code_review":   {"last_run": review_last,    "interval_secs": review_interval,    "next_in_secs": _next_in(review_last, review_interval)},
    }


# ------------------------------------------------------------------ #
#  Code Review API endpoints                                           #
# ------------------------------------------------------------------ #

@app.get("/api/research/latest")
def research_latest():
    files = sorted(glob.glob("logs/research_*.json"), reverse=True)
    if not files:
        return {"found": False}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return {"found": True, **data}
    except Exception:
        return {"found": False}


@app.get("/api/research/list")
def research_list():
    files = sorted(glob.glob("logs/research_*.json"), reverse=True)[:24]
    results = []
    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            results.append({
                "date": data.get("date", ""),
                "run_hour": data.get("run_hour", ""),
                "timestamp": data.get("timestamp", 0),
                "topics_searched": data.get("topics_searched", []),
                "finding_count": data.get("finding_count", 0),
                "high_count": data.get("high_count", 0),
                "web_search_used": data.get("web_search_used", False),
                "top_insights": data.get("top_insights", [])[:2],
            })
        except Exception:
            pass
    return results


_review_running: bool = False


@app.post("/api/code_review/run_now")
async def code_review_run_now(x_api_key: str = Header(default="")):
    if not _check_api_key(x_api_key):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    global _review_running
    if _review_running:
        return JSONResponse({"ok": False, "error": "Review already running"}, status_code=409)
    _review_running = True

    async def _run():
        global _review_running
        try:
            from src.meta_agent.code_reviewer import run_code_review
            await run_code_review()
        except Exception as exc:
            import src.utils.logger as _log
            _log.logger.warning(f"Manual code review error: {exc}")
        finally:
            _review_running = False

    asyncio.create_task(_run())
    return {"ok": True}


@app.get("/api/code_review/run_now/status")
def code_review_run_now_status():
    return {"running": _review_running}


@app.get("/api/code_review/latest")
def code_review_latest():
    files = sorted(glob.glob("logs/code_review_*.json"), reverse=True)
    if not files:
        return {"found": False}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return {"found": True, **data}
    except Exception:
        return {"found": False}


@app.get("/api/code_review/list")
def code_review_list():
    files = sorted(glob.glob("logs/code_review_*.json"), reverse=True)[:5]
    results = []
    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            results.append({
                "date": data.get("date", ""),
                "timestamp": data.get("timestamp", 0),
                "grade": data.get("grade", "?"),
                "health_score": data.get("health_score"),
                "total_findings": data.get("total_findings", 0),
                "high_findings": data.get("high_findings", 0),
                "medium_findings": data.get("medium_findings", 0),
                "summary": data.get("summary", "")[:200],
            })
        except Exception:
            pass
    return results


# ------------------------------------------------------------------ #
#  Auto-fix endpoints                                                  #
# ------------------------------------------------------------------ #

_autofix_status: dict = {"state": "idle", "results": [], "error": None, "started_at": None, "finished_at": None, "git": None}
_pending_deploys: list[dict] = []
_pending_deploys_lock = __import__("threading").Lock()


def get_pending_deploys() -> list[dict]:
    with _pending_deploys_lock:
        return list(_pending_deploys)


def clear_pending_deploys() -> None:
    with _pending_deploys_lock:
        _pending_deploys.clear()


async def _run_autofix_task() -> None:
    """Read the latest code review, then ask Claude to fix each high/medium finding."""
    global _autofix_status
    import ast as _ast
    import anthropic as _anthropic

    _autofix_status["results"] = []
    _autofix_status["error"] = None

    # Load latest review
    files = sorted(glob.glob("logs/code_review_*.json"), reverse=True)
    if not files:
        _autofix_status["state"] = "error"
        _autofix_status["error"] = "No code review found. Run the weekly review first."
        _autofix_status["finished_at"] = time.time()
        return

    try:
        with open(files[0]) as f:
            review = json.load(f)
    except Exception as e:
        _autofix_status["state"] = "error"
        _autofix_status["error"] = f"Failed to load review: {e}"
        _autofix_status["finished_at"] = time.time()
        return

    findings = [
        fi for fi in (review.get("findings") or [])
        if fi.get("severity") in ("high", "medium") and fi.get("file")
    ]

    if not findings:
        _autofix_status["state"] = "done"
        _autofix_status["results"] = [{"status": "info", "message": "No high/medium findings to fix."}]
        _autofix_status["finished_at"] = time.time()
        return

    client = _anthropic.AsyncAnthropic()

    for finding in findings:
        file_path = finding.get("file", "")
        result_entry = {
            "file": file_path,
            "title": finding.get("title", ""),
            "severity": finding.get("severity", ""),
            "status": "pending",
            "message": "",
        }
        _autofix_status["results"].append(result_entry)

        # Read source file
        abs_path = os.path.join(".", file_path)
        try:
            with open(abs_path, encoding="utf-8") as f:
                source = f.read()
        except Exception as e:
            result_entry["status"] = "skip"
            result_entry["message"] = f"Cannot read file: {e}"
            continue

        if len(source) > 12000:
            source = source[:12000] + "\n# ... (truncated)"

        prompt = f"""You are a code fixer. A code review found this issue in `{file_path}`:

**Title:** {finding.get('title', '')}
**Severity:** {finding.get('severity', '')}
**Category:** {finding.get('category', '')}
**Description:** {finding.get('description', '')}
**Suggestion:** {finding.get('suggestion', '')}

Here is the current file content:
```python
{source}
```

Respond with a JSON object (and nothing else) in this exact format:
{{
  "fixable": true,
  "old_string": "exact substring to replace (must be unique in the file)",
  "new_string": "replacement substring",
  "explanation": "one-line explanation of what was changed"
}}

If the issue is not programmatically fixable (e.g. architectural, already fixed, or requires context you lack), respond with:
{{
  "fixable": false,
  "explanation": "reason"
}}

Rules:
- old_string must appear EXACTLY once in the file
- Make the minimal change needed
- Do not add docstrings, comments, or extra formatting
- Do not rewrite the whole file"""

        try:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            fix = json.loads(raw)
        except Exception as e:
            result_entry["status"] = "error"
            result_entry["message"] = f"Claude error: {e}"
            continue

        if not fix.get("fixable"):
            result_entry["status"] = "skip"
            result_entry["message"] = fix.get("explanation", "Not fixable.")
            continue

        old_str = fix.get("old_string", "")
        new_str = fix.get("new_string", "")

        if not old_str or old_str not in source:
            result_entry["status"] = "skip"
            result_entry["message"] = "old_string not found in file (may already be fixed)."
            continue

        if source.count(old_str) > 1:
            result_entry["status"] = "skip"
            result_entry["message"] = "old_string is not unique — skipping to avoid incorrect edit."
            continue

        new_source = source.replace(old_str, new_str, 1)

        # Validate syntax if Python file
        if file_path.endswith(".py"):
            try:
                _ast.parse(new_source)
            except SyntaxError as e:
                result_entry["status"] = "error"
                result_entry["message"] = f"Syntax error after fix: {e}"
                continue

        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_source)
            result_entry["status"] = "fixed"
            result_entry["message"] = fix.get("explanation", "Fixed.")
        except Exception as e:
            result_entry["status"] = "error"
            result_entry["message"] = f"Write failed: {e}"

    # --- Commit and push any fixed files to GitHub so changes survive redeploys ---
    fixed_files = [r["file"] for r in _autofix_status["results"] if r["status"] == "fixed"]
    git_result = await _git_commit_push(fixed_files) if fixed_files else {"pushed": False, "message": "No files to commit."}
    _autofix_status["git"] = git_result
    _autofix_status["state"] = "done"
    _autofix_status["finished_at"] = time.time()


async def _git_commit_push(changed_files: list[str]) -> dict:
    """
    Commit changed files and push to GitHub using GITHUB_TOKEN env var.
    Returns a dict with 'pushed' (bool) and 'message' (str).
    """
    import subprocess as _sp

    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return {
            "pushed": False,
            "message": "GITHUB_TOKEN not set — fixes written to disk only. Add GITHUB_TOKEN to Railway env vars to enable auto-push.",
        }

    try:
        def _run(cmd: list[str], **kwargs) -> _sp.CompletedProcess:
            return _sp.run(cmd, capture_output=True, text=True, timeout=30, **kwargs)

        # Configure git identity (required for commit)
        _run(["git", "config", "user.email", "autofix-bot@railway.local"])
        _run(["git", "config", "user.name", "AutoFix Bot"])

        # Inject token into remote URL
        url_res = _run(["git", "remote", "get-url", "origin"])
        original_url = url_res.stdout.strip()
        auth_url = original_url.replace("https://", f"https://{token}@")
        _run(["git", "remote", "set-url", "origin", auth_url])

        # Stage only the fixed files
        for f in changed_files:
            _run(["git", "add", f])

        n = len(changed_files)
        files_str = ", ".join(changed_files)
        commit_res = _run(["git", "commit", "-m",
            f"autofix: apply {n} code review fix(es) via dashboard\n\nFixed files: {files_str}"])

        if commit_res.returncode != 0:
            # Nothing to commit (fixes may already be staged/identical)
            _run(["git", "remote", "set-url", "origin", original_url])
            return {"pushed": False, "message": f"Git commit failed: {commit_res.stderr.strip() or 'nothing to commit'}"}

        push_res = _run(["git", "push", "origin", "HEAD"])

        # Always restore clean remote URL (strip token)
        _run(["git", "remote", "set-url", "origin", original_url])

        if push_res.returncode == 0:
            return {"pushed": True, "message": f"Pushed {n} fix(es) to GitHub. Railway redeploy triggered."}
        else:
            return {"pushed": False, "message": f"Push failed: {push_res.stderr.strip()}"}

    except Exception as exc:
        return {"pushed": False, "message": f"Git error: {exc}"}


@app.post("/api/code_review/autofix")
async def code_review_autofix(x_api_key: str = Header(default="")):
    if not _check_api_key(x_api_key):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    global _autofix_status
    if _autofix_status.get("state") == "running":
        return JSONResponse({"ok": False, "error": "Already running"}, status_code=409)
    _autofix_status = {
        "state": "running",
        "results": [],
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    asyncio.create_task(_run_autofix_task())
    return {"ok": True}


@app.get("/api/code_review/autofix/status")
def code_review_autofix_status():
    return _autofix_status


# ------------------------------------------------------------------ #
#  Research signals + strategy proposals                              #
# ------------------------------------------------------------------ #

@app.get("/api/research/signals")
def research_signals():
    try:
        with open("logs/research_signals.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"active_topics": [], "strategy_focus": None, "param_hints": {}}


@app.get("/api/research/proposals/list")
def research_proposals_list():
    import glob as _glob
    metas = sorted(_glob.glob("logs/proposals/*_meta.json"), reverse=True)[:20]
    results = []
    for mp in metas:
        try:
            with open(mp) as f:
                results.append(json.load(f))
        except Exception:
            pass
    return results


@app.get("/api/research/proposals/{proposal_id}")
def research_proposal_detail(proposal_id: str):
    import glob as _glob
    # Find meta file by id
    for mp in _glob.glob("logs/proposals/*_meta.json"):
        try:
            with open(mp) as f:
                meta = json.load(f)
            if meta.get("id") == proposal_id:
                # Load code
                code = ""
                try:
                    with open(meta["file_path"], encoding="utf-8") as f:
                        code = f.read()
                except Exception:
                    pass
                return {**meta, "code": code}
        except Exception:
            pass
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.post("/api/research/proposals/{proposal_id}/deploy")
async def deploy_proposal(proposal_id: str, x_api_key: str = Header(default="")):
    if not _check_api_key(x_api_key):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    import ast as _ast
    import glob as _glob
    import shutil as _shutil

    for mp in _glob.glob("logs/proposals/*_meta.json"):
        try:
            with open(mp) as f:
                meta = json.load(f)
        except Exception:
            continue
        if meta.get("id") != proposal_id:
            continue

        if meta.get("deployed"):
            return JSONResponse({"ok": False, "error": "Already deployed"}, status_code=409)

        # Load and validate code
        try:
            with open(meta["file_path"], encoding="utf-8") as f:
                code = f.read()
            _ast.parse(code)
        except SyntaxError as exc:
            return JSONResponse({"ok": False, "error": f"Syntax error: {exc}"}, status_code=400)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        # Copy to src/strategies/
        dest = os.path.join("src", "strategies", meta["file_name"])
        try:
            _shutil.copy2(meta["file_path"], dest)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"Copy failed: {exc}"}, status_code=500)

        # Mark deployed
        meta["deployed"] = True
        meta["deployed_at"] = time.time()
        meta["deployed_path"] = dest
        with open(mp, "w") as f:
            json.dump(meta, f, indent=2)

        # Queue for hot-load by main bot loop (lock for cross-thread safety)
        with _pending_deploys_lock:
            _pending_deploys.append({
                "proposal_id": proposal_id,
                "class_name": meta["class_name"],
                "file_path": dest,
                "deployed_at": meta["deployed_at"],
            })

        return {"ok": True, "deployed_path": dest, "class_name": meta["class_name"]}

    return JSONResponse({"error": "Proposal not found"}, status_code=404)


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _fmt_uptime(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s"


# ------------------------------------------------------------------ #
#  Dashboard HTML                                                      #
# ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Arb Bot</title>

<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#e0e0e0;font-family:'Segoe UI',sans-serif;font-size:13px}
header{background:#111;border-bottom:1px solid #222;padding:12px 20px;display:flex;align-items:center;gap:16px}
header h1{color:#00e5ff;font-size:1.1rem;letter-spacing:.05em}
#mode-badge{font-size:.7rem;padding:3px 10px;border-radius:4px;background:#1a3a4a;color:#00e5ff}
#uptime-info{color:#555;font-size:.75rem;margin-left:auto}
#reset-btn{font-size:.7rem;padding:4px 12px;border-radius:4px;background:#1a0000;color:#ff5252;border:1px solid #3d0000;cursor:pointer;transition:all .2s}
#reset-btn:hover{background:#3d0000}

.tabs{display:flex;background:#111;border-bottom:1px solid #1e1e1e;padding:0 20px;flex-wrap:wrap}
.tab{padding:10px 18px;cursor:pointer;color:#666;font-size:.8rem;border-bottom:2px solid transparent;transition:all .2s}
.tab:hover{color:#aaa}
.tab.active{color:#00e5ff;border-bottom-color:#00e5ff}

.page{display:none;padding:20px;animation:fadein .2s}
.page.active{display:block}
@keyframes fadein{from{opacity:0}to{opacity:1}}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:18px}
.card{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px}
.card .lbl{color:#555;font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.card .val{font-size:1.3rem;font-weight:700;color:#fff}
.card .sub{color:#444;font-size:.65rem;margin-top:3px}
.card .val.green{color:#00e676}.card .val.red{color:#ff5252}.card .val.blue{color:#00e5ff}.card .val.yellow{color:#ffd740}.card .val.purple{color:#ce93d8}

.pnl-split{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px;margin-bottom:18px}
.pnl-split h3{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:12px}
.pnl-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #1a1a1a}
.pnl-row:last-child{border-bottom:none}
.pnl-label{color:#888;font-size:.78rem}
.pnl-value{font-size:.95rem;font-weight:700}
.pnl-note{color:#444;font-size:.65rem;margin-top:2px}

.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}
.chart-box{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px}
.chart-box h3{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px}
.chart-box canvas{max-height:200px}

.section{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px;margin-bottom:14px}
.section h3{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:#444;font-weight:500;padding:5px 8px;border-bottom:1px solid #1e1e1e;font-size:.7rem}
td{padding:5px 8px;border-bottom:1px solid #141414;font-size:.75rem}
tr:last-child td{border-bottom:none}
tr:hover td{background:#181818}
.buy{color:#00e676}.sell{color:#ff5252}.win{color:#00e676}.loss{color:#ff5252}
.badge{display:inline-block;padding:1px 7px;border-radius:3px;font-size:.65rem;font-weight:600}
.badge.rebalancing{background:#1a2a1a;color:#00e676}
.badge.combinatorial{background:#1a1a2a;color:#7986cb}
.badge.latency_arb{background:#2a1a1a;color:#ff7043}
.badge.market_making{background:#2a2a1a;color:#ffd740}
.badge.resolution{background:#1a2a2a;color:#4dd0e1}
.badge.event_driven{background:#2a1a2a;color:#ce93d8}

.strat-bars{display:flex;flex-direction:column;gap:8px}
.strat-row{display:flex;align-items:center;gap:10px}
.strat-row .name{width:150px;font-size:.75rem;color:#888}
.strat-row .bar-wrap{flex:1;background:#0d0d0d;border-radius:4px;height:20px;overflow:hidden}
.strat-row .bar{height:100%;border-radius:4px;display:flex;align-items:center;padding:0 8px;font-size:.7rem;font-weight:700;min-width:50px;transition:width .5s}
.bar.pos{background:#003d1a;color:#00e676}.bar.neg{background:#3d0000;color:#ff5252}

#log-feed{background:#0d0d0d;border:1px solid #1e1e1e;border-radius:8px;height:500px;overflow-y:auto;padding:10px;font-family:monospace;font-size:.72rem}
.log-line{padding:1px 0;border-bottom:1px solid #111;line-height:1.5}
.log-line .ts{color:#444;margin-right:8px}
.log-line .lvl{margin-right:8px;font-weight:700}
.log-line .lvl.INFO{color:#00e5ff}.log-line .lvl.WARNING{color:#ffd740}.log-line .lvl.ERROR{color:#ff5252}.log-line .lvl.DEBUG{color:#555}.log-line .lvl.SUCCESS{color:#00e676}

.meta-card{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:16px;margin-bottom:14px}
.meta-card h3{color:#7986cb;margin-bottom:8px;font-size:.85rem}
.meta-analysis{color:#ccc;font-size:.78rem;line-height:1.6;white-space:pre-wrap;max-height:300px;overflow-y:auto}
.change-table td:nth-child(3){color:#00e676}
.ts-small{color:#555;font-size:.65rem}
.no-data{color:#333;text-align:center;padding:30px;font-size:.8rem}
#last-update{color:#333;font-size:.65rem;text-align:right;padding:6px 20px}

/* Status tab */
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:18px}
.status-item{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:12px;display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.dot.ok{background:#00e676;box-shadow:0 0 6px #00e676}
.dot.warn{background:#ffd740;box-shadow:0 0 6px #ffd740}
.dot.err{background:#ff5252;box-shadow:0 0 6px #ff5252}
.status-label{font-size:.78rem;color:#ccc}
.status-detail{font-size:.65rem;color:#555;margin-top:2px}

.strat-card{background:#141414;border:2px solid #1e1e1e;border-radius:8px;padding:12px}
.strat-card.enabled{border-color:#1a3a1a}
.strat-card.disabled{border-color:#2a1a1a;opacity:.6}
.strat-card h4{font-size:.8rem;margin-bottom:4px}
.strat-card .strat-status{font-size:.65rem;font-weight:700}
.strat-card .strat-note{font-size:.65rem;color:#555;margin-top:4px}

.health-bar{height:8px;border-radius:4px;background:#1a1a1a;overflow:hidden;margin-top:6px}
.health-fill{height:100%;border-radius:4px;transition:width .5s}

/* Analytics tab */
.analytics-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.thinking-card{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px;margin-bottom:14px}
.thinking-card h3{font-size:.72rem;color:#7986cb;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px}

/* Time-to-real-PnL widget */
.ttpl-card{background:#111;border:1px solid #1e2a1e;border-top:3px solid #00e676;border-radius:8px;padding:16px;margin-bottom:18px}
.ttpl-header{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.ttpl-title{font-size:.85rem;font-weight:700;color:#ccc;text-transform:uppercase;letter-spacing:.06em}
.ttpl-badge{font-size:.7rem;font-weight:700;padding:3px 10px;border-radius:4px;background:#1a2a1a;color:#00e676}
.ttpl-badge.bootstrap{background:#2a1a00;color:#ffd740}
.ttpl-badge.active{background:#001a0a;color:#00e676}
.ttpl-badge.profit{background:#001a1a;color:#00e5ff}
.ttpl-milestones{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:12px}
.ttpl-milestone{background:#141414;border:1px solid #1e1e1e;border-radius:6px;padding:12px}
.ttpl-milestone.done{border-color:#1a3a1a}
.ttpl-milestone.done .ttpl-ms-eta{color:#00e676}
.ttpl-ms-label{font-size:.65rem;color:#555;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}
.ttpl-ms-eta{font-size:1.1rem;font-weight:700;color:#ffd740;margin-bottom:4px}
.ttpl-ms-sub{font-size:.65rem;color:#444}
.ttpl-ms-bar{height:4px;background:#1a1a1a;border-radius:2px;margin-top:8px;overflow:hidden}
.ttpl-ms-fill{height:100%;border-radius:2px;transition:width .5s}
.ttpl-verdict{font-size:.78rem;color:#666;line-height:1.6;padding-top:8px;border-top:1px solid #1a1a1a}

/* Balances tab */
.bal-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-bottom:18px}
.bal-card{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:18px}
.bal-card.anthropic{border-top:3px solid #ce93d8}
.bal-card.railway{border-top:3px solid #4dd0e1}
.bal-card.bot{border-top:3px solid #00e676}
.bal-card h3{font-size:.8rem;font-weight:700;margin-bottom:14px;text-transform:uppercase;letter-spacing:.06em}
.bal-card.anthropic h3{color:#ce93d8}
.bal-card.railway h3{color:#4dd0e1}
.bal-card.bot h3{color:#00e676}
.bal-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid #1a1a1a;font-size:.78rem}
.bal-row:last-child{border-bottom:none}
.bal-lbl{color:#555}
.bal-val{font-weight:600;color:#ccc}
.budget-bar{height:6px;border-radius:3px;background:#1a1a1a;overflow:hidden;margin-top:10px}
.budget-fill{height:100%;border-radius:3px;transition:width .5s}
.budget-label{font-size:.65rem;color:#555;margin-top:4px;display:flex;justify-content:space-between}
.cycle-bar{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:16px;margin-bottom:18px}
.cycle-bar h3{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:12px}
.cycle-progress{height:10px;border-radius:5px;background:#1a1a1a;overflow:hidden;margin-bottom:8px}
.cycle-fill{height:100%;border-radius:5px;background:linear-gradient(90deg,#00e5ff,#7986cb);transition:width .5s}
.refill-link{display:block;margin-top:12px;text-align:center;font-size:.72rem;font-weight:600;padding:7px;border-radius:5px;background:#1a1a1a;border:1px solid #2a2a2a;color:#888;text-decoration:none;transition:all .2s}
.refill-link:hover{background:#222;color:#ccc;border-color:#444}

/* Research tab */
.res-insights-card{background:#111;border:1px solid #2a2a1a;border-top:3px solid #ffd740;border-radius:8px;padding:16px;margin-bottom:14px}
.res-insights-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.res-run-label{font-size:.65rem;color:#555}
.res-insight{display:flex;align-items:flex-start;gap:10px;padding:7px 0;border-bottom:1px solid #1a1a1a;font-size:.78rem;color:#ccc;line-height:1.5}
.res-insight:last-child{border-bottom:none}
.res-insight-bullet{color:#ffd740;font-size:.9rem;flex-shrink:0;margin-top:1px}
.res-finding{background:#111;border:1px solid #1e1e1e;border-radius:6px;padding:12px;margin-bottom:8px;border-left:3px solid #333}
.res-finding.high{border-left-color:#00e676}
.res-finding.medium{border-left-color:#ffd740}
.res-finding.low{border-left-color:#555}
.res-finding-meta{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.res-rel{font-size:.65rem;font-weight:700;padding:2px 7px;border-radius:3px;text-transform:uppercase}
.res-rel.high{background:#001a0a;color:#00e676}
.res-rel.medium{background:#2a2a00;color:#ffd740}
.res-rel.low{background:#1a1a1a;color:#555}
.res-cat{font-size:.65rem;background:#1a1a2a;color:#7986cb;padding:2px 7px;border-radius:3px}
.res-source{font-size:.65rem;color:#555;font-style:italic}
.res-title{font-size:.82rem;font-weight:600;color:#ddd;margin-bottom:4px}
.res-summary{font-size:.75rem;color:#888;line-height:1.5;margin-bottom:5px}
.res-suggestion{font-size:.72rem;color:#00e5ff;padding:5px 8px;background:#001a1a;border-radius:4px}
.res-experiment{display:flex;align-items:flex-start;gap:8px;padding:7px 0;border-bottom:1px solid #1a1a1a;font-size:.78rem;color:#ccc}
.res-experiment:last-child{border-bottom:none}
.res-exp-num{color:#7986cb;font-weight:700;flex-shrink:0;min-width:18px}

/* Code Review tab */
.cr-finding{background:#111;border:1px solid #1e1e1e;border-radius:6px;padding:12px;margin-bottom:8px;border-left:3px solid #333}
.cr-finding.high{border-left-color:#ff5252}
.cr-finding.medium{border-left-color:#ffd740}
.cr-finding.low{border-left-color:#00e5ff}
.cr-finding.info{border-left-color:#555}
.cr-finding-header{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.cr-sev{font-size:.65rem;font-weight:700;padding:2px 7px;border-radius:3px;text-transform:uppercase}
.cr-sev.high{background:#3d0000;color:#ff5252}
.cr-sev.medium{background:#3d3000;color:#ffd740}
.cr-sev.low{background:#001a3d;color:#00e5ff}
.cr-sev.info{background:#1a1a1a;color:#666}
.cr-cat{font-size:.65rem;color:#555;font-style:italic}
.cr-file{font-size:.65rem;color:#7986cb;font-family:monospace}
.cr-title{font-size:.82rem;font-weight:600;color:#ddd}
.cr-desc{font-size:.75rem;color:#888;margin-top:4px;line-height:1.5}
.cr-suggestion{font-size:.72rem;color:#4dd0e1;margin-top:5px;padding:5px 8px;background:#001a1a;border-radius:4px}
.cr-strength{font-size:.78rem;color:#00e676;padding:4px 0;display:flex;align-items:flex-start;gap:6px}
.cr-grade-A{color:#00e676}.cr-grade-B{color:#00e5ff}.cr-grade-C{color:#ffd740}.cr-grade-D{color:#ff7043}.cr-grade-F{color:#ff5252}
</style>
</head>
<body>

<header>
  <h1>Polymarket Arb Bot</h1>
  <span id="mode-badge">PAPER</span>
  <span id="uptime-info">loading...</span>
  <button id="reset-btn" onclick="resetPortfolio()">Reset to $10,000</button>
</header>

<div class="tabs">
  <div class="tab active" onclick="showTab('overview')">Overview</div>
  <div class="tab" onclick="showTab('live')">Live Feed</div>
  <div class="tab" onclick="showTab('positions')">Positions</div>
  <div class="tab" onclick="showTab('trades')">Trades</div>
  <div class="tab" onclick="showTab('status')">Status</div>
  <div class="tab" onclick="showTab('analytics')">Analytics</div>
  <div class="tab" onclick="showTab('balances')">Balances</div>
  <div class="tab" onclick="showTab('meta')">Meta-Agent</div>
  <div class="tab" onclick="showTab('codereview')">Code Review</div>
  <div class="tab" onclick="showTab('research')">Research</div>
</div>

<!-- OVERVIEW TAB -->
<div class="page active" id="tab-overview">
  <div class="cards">
    <div class="card"><div class="lbl">Cash Balance</div><div class="val blue" id="balance">--</div></div>
    <div class="card"><div class="lbl">Total Value</div><div class="val" id="total-value">--</div><div class="sub" id="total-pnl-sub">--</div></div>
    <div class="card">
      <div class="lbl">Realized P&amp;L ✓</div>
      <div class="val" id="realized-pnl">--</div>
      <div class="sub" id="realized-pnl-pct">-- | <span id="closed-count">0</span> closed</div>
    </div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val" id="win-rate">--</div><div class="sub">closed positions</div></div>
    <div class="card"><div class="lbl">Open Positions</div><div class="val yellow" id="pos-count">--</div><div class="sub" id="exposure-sub">--</div></div>
    <div class="card"><div class="lbl">Trades / hr</div><div class="val purple" id="trades-per-hr">--</div><div class="sub" id="total-trades-sub">-- total</div></div>
    <div class="card"><div class="lbl">Fees Paid</div><div class="val red" id="fees">--</div></div>
  </div>

  <div class="chart-grid">
    <div class="chart-box">
      <h3>Portfolio Value Over Time</h3>
      <canvas id="pnlChart"></canvas>
    </div>
    <div class="chart-box">
      <h3>Trades Per Strategy</h3>
      <canvas id="stratChart"></canvas>
    </div>
  </div>

  <div class="section">
    <h3>Strategy P&L</h3>
    <div class="strat-bars" id="strat-bars"><div class="no-data">Waiting for trades...</div></div>
  </div>

  <!-- Agent countdown timers -->
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px" id="agent-timers">
    <div style="background:#111;border-radius:6px;padding:12px 14px;border-left:3px solid #7c4dff">
      <div style="font-size:.62rem;text-transform:uppercase;letter-spacing:.07em;color:#7c4dff;font-weight:700;margin-bottom:4px">Meta-Agent</div>
      <div style="font-size:1.1rem;font-weight:700;font-family:monospace;color:#e0e0e0" id="timer-meta">--</div>
      <div style="font-size:.62rem;color:#555;margin-top:2px" id="timer-meta-sub">last run --</div>
    </div>
    <div style="background:#111;border-radius:6px;padding:12px 14px;border-left:3px solid #00bcd4">
      <div style="font-size:.62rem;text-transform:uppercase;letter-spacing:.07em;color:#00bcd4;font-weight:700;margin-bottom:4px">Research Agent</div>
      <div style="font-size:1.1rem;font-weight:700;font-family:monospace;color:#e0e0e0" id="timer-research">--</div>
      <div style="font-size:.62rem;color:#555;margin-top:2px" id="timer-research-sub">last run --</div>
    </div>
    <div style="background:#111;border-radius:6px;padding:12px 14px;border-left:3px solid #ff9800">
      <div style="font-size:.62rem;text-transform:uppercase;letter-spacing:.07em;color:#ff9800;font-weight:700;margin-bottom:4px">Code Review</div>
      <div style="font-size:1.1rem;font-weight:700;font-family:monospace;color:#e0e0e0" id="timer-review">--</div>
      <div style="font-size:.62rem;color:#555;margin-top:2px" id="timer-review-sub">last run --</div>
    </div>
  </div>
</div>

<!-- LIVE FEED TAB -->
<div class="page" id="tab-live">
  <div class="cards" style="grid-template-columns:repeat(5,1fr);margin-bottom:14px">
    <div class="card"><div class="lbl">Cycle</div><div class="val blue" id="live-cycle">--</div></div>
    <div class="card"><div class="lbl">Uptime</div><div class="val" id="live-uptime">--</div></div>
    <div class="card"><div class="lbl">Realized P&amp;L</div><div class="val" id="live-realized">--</div></div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val" id="live-winrate">--</div></div>
    <div class="card"><div class="lbl">Trades</div><div class="val" id="live-trades">--</div></div>
  </div>
  <div class="section">
    <h3>Bot Log Stream <span style="color:#555;font-weight:normal">(last 500 lines)</span>
      <label style="float:right;color:#555;font-size:.7rem"><input type="checkbox" id="autoscroll" checked> Auto-scroll</label>
    </h3>
    <div id="log-feed"></div>
  </div>
</div>

<!-- POSITIONS TAB -->
<div class="page" id="tab-positions">
  <div class="section">
    <h3>Open Positions (<span id="open-pos-count">0</span>)</h3>
    <div id="positions-table"><div class="no-data">No open positions</div></div>
  </div>
  <div class="section">
    <h3>Closed Positions — Recent 100 <span style="color:#555;font-weight:normal;font-size:.65rem">These are REAL results</span></h3>
    <div id="closed-table"><div class="no-data">No closed positions yet</div></div>
  </div>
</div>

<!-- TRADES TAB -->
<div class="page" id="tab-trades">
  <div class="cards" style="grid-template-columns:repeat(4,1fr);margin-bottom:14px">
    <div class="card"><div class="lbl">Total Trades</div><div class="val blue" id="t-total">--</div></div>
    <div class="card"><div class="lbl">Buy Trades</div><div class="val green" id="t-buys">--</div></div>
    <div class="card"><div class="lbl">Sell Trades</div><div class="val red" id="t-sells">--</div></div>
    <div class="card"><div class="lbl">Fees Paid</div><div class="val yellow" id="t-fees">--</div></div>
  </div>
  <div class="section">
    <h3>Recent Trades (last 100)</h3>
    <div id="trades-table"><div class="no-data">No trades yet</div></div>
  </div>
</div>

<!-- STATUS TAB -->
<div class="page" id="tab-status">
  <div class="section">
    <h3>System Connections</h3>
    <div class="status-grid" id="status-connections">
      <div class="no-data">Loading...</div>
    </div>
  </div>

  <div class="section">
    <h3>Strategies</h3>
    <div class="status-grid" id="status-strategies">
      <div class="no-data">Loading...</div>
    </div>
  </div>

  <div class="section">
    <h3>Risk Health</h3>
    <div id="status-risk"><div class="no-data">Loading...</div></div>
  </div>

  <div class="section">
    <h3>Disk Usage</h3>
    <div id="status-disk"><div class="no-data">Loading...</div></div>
  </div>
</div>

<!-- ANALYTICS TAB -->
<div class="page" id="tab-analytics">

  <!-- Time-to-real-PnL estimator -->
  <div class="ttpl-card" id="ttpl-card">
    <div class="ttpl-header">
      <span class="ttpl-title">Time to Real P&amp;L</span>
      <span class="ttpl-badge" id="ttpl-phase-badge">--</span>
    </div>
    <div class="ttpl-body">
      <div class="ttpl-milestones" id="ttpl-milestones">
        <div class="no-data">Loading estimates...</div>
      </div>
      <div class="ttpl-verdict" id="ttpl-verdict"></div>
    </div>
  </div>

  <!-- Row 1: ROI + Win Rate -->
  <div class="analytics-row">
    <div class="chart-box">
      <h3>Strategy ROI %</h3>
      <canvas id="roiChart" style="max-height:220px"></canvas>
    </div>
    <div class="chart-box">
      <h3>Strategy Win Rate %</h3>
      <canvas id="winRateChart" style="max-height:220px"></canvas>
    </div>
  </div>

  <!-- Row 2: Hourly PnL + Fee Drag -->
  <div class="analytics-row">
    <div class="chart-box">
      <h3>Hourly PnL — Last 24h</h3>
      <canvas id="hourlyPnlChart" style="max-height:220px"></canvas>
    </div>
    <div class="chart-box">
      <h3>Fee Drag Per Strategy</h3>
      <canvas id="feeDragChart" style="max-height:220px"></canvas>
    </div>
  </div>

  <!-- Row 3: Health score trend -->
  <div class="section">
    <h3>Health Score Trend (Meta-Agent History)</h3>
    <canvas id="healthTrendChart" style="max-height:160px"></canvas>
  </div>

  <!-- Row 4: Bot Thinking -->
  <div class="thinking-card">
    <h3>Bot Thinking — Recent LLM Decisions</h3>
    <div id="llm-decisions-table"><div class="no-data">No LLM-tagged trades yet</div></div>
  </div>

  <div class="thinking-card">
    <h3>Active LLM Signals (Last Hour)</h3>
    <div id="llm-active-signals"><div class="no-data">None in last hour</div></div>
  </div>

  <!-- Row 5: Parameter change timeline -->
  <div class="thinking-card">
    <h3>Meta-Agent Parameter Change Timeline</h3>
    <div id="param-timeline"><div class="no-data">No parameter changes yet</div></div>
  </div>
</div>

<!-- BALANCES TAB -->
<div class="page" id="tab-balances">

  <!-- Billing cycle bar -->
  <div class="cycle-bar">
    <h3>Billing Cycle</h3>
    <div id="cycle-dates" style="display:flex;justify-content:space-between;font-size:.72rem;color:#555;margin-bottom:8px">
      <span id="cycle-start">--</span><span id="cycle-days-left" style="color:#aaa">-- days remaining</span><span id="cycle-end">--</span>
    </div>
    <div class="cycle-progress"><div class="cycle-fill" id="cycle-fill" style="width:0%"></div></div>
    <div style="font-size:.65rem;color:#444;margin-top:4px;text-align:center"><span id="cycle-pct">0</span>% of billing cycle elapsed</div>
  </div>

  <!-- Service cards -->
  <div class="bal-grid">

    <!-- Anthropic -->
    <div class="bal-card anthropic">
      <h3>Anthropic API</h3>
      <div class="bal-row"><span class="bal-lbl">Model</span><span class="bal-val" id="bal-ant-model">--</span></div>
      <div class="bal-row"><span class="bal-lbl">API Key</span><span class="bal-val" id="bal-ant-key">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Meta-Agent Runs (this deploy)</span><span class="bal-val" id="bal-ant-runs">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Est. Cost / Run</span><span class="bal-val" id="bal-ant-cpr">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Est. Spend (this deploy)</span><span class="bal-val" id="bal-ant-cost" style="color:#ce93d8">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Projected Monthly</span><span class="bal-val" id="bal-ant-proj">--</span></div>
      <div class="bal-row" id="bal-ant-budget-row"><span class="bal-lbl">Monthly Budget</span><span class="bal-val" id="bal-ant-budget">Not set</span></div>
      <div class="budget-bar"><div class="budget-fill" id="bal-ant-bar" style="width:0%;background:#ce93d8"></div></div>
      <div class="budget-label"><span id="bal-ant-bar-lbl">Set ANTHROPIC_MONTHLY_BUDGET env var to track</span><span id="bal-ant-bar-pct"></span></div>
      <a href="https://console.anthropic.com/settings/billing" target="_blank" class="refill-link">+ Add Anthropic Credits →</a>
    </div>

    <!-- Railway -->
    <div class="bal-card railway">
      <h3>Railway</h3>
      <div class="bal-row"><span class="bal-lbl">Plan Base Cost</span><span class="bal-val" id="bal-rail-base">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Days Left in Cycle</span><span class="bal-val" id="bal-rail-days">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Disk Used / Limit</span><span class="bal-val" id="bal-rail-disk">--</span></div>
      <div class="budget-bar"><div class="budget-fill" id="bal-rail-bar" style="width:0%;background:#4dd0e1"></div></div>
      <div class="budget-label"><span>Disk usage</span><span id="bal-rail-bar-pct"></span></div>
      <div style="font-size:.68rem;color:#555;margin-top:10px">Live billing data (credit remaining, monthly spend) must be checked directly on Railway — their API does not expose it.</div>
      <a href="https://railway.app/account/billing" target="_blank" class="refill-link">+ View Billing on Railway →</a>
    </div>

    <!-- Bot Stats -->
    <div class="bal-card bot">
      <h3>Bot Runtime</h3>
      <div class="bal-row"><span class="bal-lbl">Mode</span><span class="bal-val" id="bal-bot-mode">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Uptime This Deploy</span><span class="bal-val" id="bal-bot-uptime">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Trades Executed</span><span class="bal-val" id="bal-bot-trades">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Portfolio P&amp;L</span><span class="bal-val" id="bal-bot-pnl">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Cycles Left (billing)</span><span class="bal-val" id="bal-bot-cycles">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Meta-Runs Left (billing)</span><span class="bal-val" id="bal-bot-metaruns">--</span></div>
      <a href="https://polymarket.com/wallet" target="_blank" class="refill-link">+ Deposit USDC to Polymarket →</a>
    </div>

  </div>

  <!-- Cost breakdown note -->
  <div class="section">
    <h3>Cost Breakdown Notes</h3>
    <div style="font-size:.75rem;color:#555;line-height:1.8">
      <div><span style="color:#ce93d8">Anthropic:</span> Claude Opus 4.6 with extended thinking — ~$0.79/meta-agent run (est. 2,500 input tokens + 10,000 output/thinking). Runs every 30 min when active.</div>
      <div style="margin-top:6px"><span style="color:#4dd0e1">Railway:</span> Hobby plan $5/mo base + usage. "Credit Remaining" and "Days Left" come from Railway's API — requires <code style="color:#666">RAILWAY_TOKEN</code>. Get your token at railway.app → Account → Tokens.</div>
      <div style="margin-top:6px"><span style="color:#00e676">Volume:</span> Set <code style="color:#666">RAILWAY_VOLUME_LIMIT_MB</code> if your volume size differs from 512 MB default.</div>
      <div style="margin-top:6px"><span style="color:#ffd740">Budgets:</span> Set <code style="color:#666">ANTHROPIC_MONTHLY_BUDGET</code> env var to show Anthropic budget bar.</div>
    </div>
  </div>
</div>

<!-- META-AGENT TAB -->
<div class="page" id="tab-meta">
  <div class="cards" style="grid-template-columns:repeat(3,1fr);margin-bottom:14px">
    <div class="card"><div class="lbl">Analyses Run</div><div class="val blue" id="meta-count">--</div></div>
    <div class="card"><div class="lbl">Last Run</div><div class="val" id="meta-last">--</div></div>
    <div class="card"><div class="lbl">Next Run</div><div class="val yellow" id="meta-next">--</div></div>
  </div>
  <div id="meta-latest-card">
    <div class="no-data">No meta-agent analysis yet.</div>
  </div>
  <div class="section" style="margin-top:14px">
    <h3>Analysis History</h3>
    <div id="meta-history"></div>
  </div>
</div>

<!-- CODE REVIEW TAB -->
<div class="page" id="tab-codereview">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <button id="cr-runnow-btn" onclick="runCodeReviewNow()" style="padding:6px 16px;background:#1b5e20;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:.75rem;font-weight:700">▶ Run Review Now</button>
    <span id="cr-runnow-status" style="font-size:.7rem;color:#555"></span>
  </div>
  <div class="cards" style="grid-template-columns:repeat(4,1fr);margin-bottom:14px">
    <div class="card"><div class="lbl">Code Grade</div><div class="val blue" id="cr-grade">--</div></div>
    <div class="card"><div class="lbl">Health Score</div><div class="val" id="cr-score">--</div></div>
    <div class="card"><div class="lbl">Last Review</div><div class="val yellow" id="cr-date">--</div></div>
    <div class="card"><div class="lbl">Findings</div><div class="val" id="cr-total">--</div><div class="sub" id="cr-severity">--</div></div>
  </div>

  <div class="meta-card" id="cr-summary-card">
    <h3>Summary</h3>
    <div id="cr-summary" class="meta-analysis">No review yet — runs automatically once a week (first run 5 minutes after bot start).</div>
  </div>

  <div class="meta-card" id="cr-strengths-card" style="display:none">
    <h3>Strengths</h3>
    <div id="cr-strengths"></div>
  </div>

  <div class="section" style="margin-top:14px">
    <h3>Findings</h3>
    <div id="cr-findings"><div class="no-data">No findings yet.</div></div>
  </div>

  <div class="section" style="margin-top:14px">
    <h3>Review History</h3>
    <div id="cr-history"><div class="no-data">No history yet.</div></div>
  </div>

  <!-- Auto-Fix Panel -->
  <div class="meta-card" style="margin-top:14px" id="cr-autofix-card">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h3 style="margin:0">Auto-Fix with Claude</h3>
      <button id="cr-autofix-btn" onclick="triggerAutofix()" style="padding:6px 16px;background:#1565c0;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:.75rem;font-weight:700">
        ▶ Run Auto-Fix
      </button>
      <span id="cr-autofix-status" style="font-size:.7rem;color:#555"></span>
    </div>
    <div style="font-size:.68rem;color:#555;margin-top:4px">
      Sends each high &amp; medium finding to Claude Sonnet, which applies targeted edits and validates syntax.
    </div>
    <div id="cr-autofix-results" style="margin-top:10px"></div>
  </div>
</div>

<!-- RESEARCH TAB -->
<div class="page" id="tab-research">

  <div class="cards" style="grid-template-columns:repeat(4,1fr);margin-bottom:14px">
    <div class="card"><div class="lbl">Last Run</div><div class="val blue" id="res-last">--</div></div>
    <div class="card"><div class="lbl">Next Run</div><div class="val yellow" id="res-next">--</div></div>
    <div class="card"><div class="lbl">Total Findings</div><div class="val" id="res-total">--</div><div class="sub" id="res-high">--</div></div>
    <div class="card"><div class="lbl">Web Search</div><div class="val" id="res-websearch">--</div><div class="sub" id="res-interval">every -- h</div></div>
  </div>

  <!-- Active Research Signals -->
  <div class="meta-card" id="res-signals-card" style="margin-bottom:14px">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">
      <h3 style="margin:0">Active Research Signals</h3>
      <span id="res-signals-status" style="font-size:.68rem;color:#555">injected into combinatorial strategy</span>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <span style="font-size:.68rem;color:#888;text-transform:uppercase;letter-spacing:.05em">Hot topics:</span>
      <div id="res-active-topics" style="display:flex;gap:6px;flex-wrap:wrap"><span style="color:#555;font-size:.68rem">none yet</span></div>
    </div>
    <div style="margin-top:6px;display:flex;gap:16px;flex-wrap:wrap">
      <span style="font-size:.68rem"><span style="color:#888">Strategy focus:</span> <span id="res-signal-focus" style="color:#ffd740">--</span></span>
      <span style="font-size:.68rem"><span style="color:#888">Confidence:</span> <span id="res-signal-confidence">--</span></span>
      <span style="font-size:.68rem"><span style="color:#888">Param hints:</span> <span id="res-signal-params" style="font-family:monospace;color:#90caf9">none</span></span>
    </div>
  </div>

  <!-- Top insights from latest run -->
  <div class="res-insights-card" id="res-insights-card">
    <div class="res-insights-header">
      <span style="font-size:.8rem;font-weight:700;color:#ffd740;text-transform:uppercase;letter-spacing:.06em">Top Insights</span>
      <span class="res-run-label" id="res-run-label">--</span>
    </div>
    <div id="res-insights"><div class="no-data">No research yet — runs every 2 hours (configurable via RESEARCH_INTERVAL_HOURS)</div></div>
  </div>

  <!-- Findings list -->
  <div class="section" style="margin-top:14px">
    <h3>Findings <span style="color:#555;font-weight:normal;font-size:.65rem" id="res-topics-label"></span></h3>
    <div id="res-findings"><div class="no-data">No findings yet.</div></div>
  </div>

  <!-- Suggested experiments -->
  <div class="section" style="margin-top:14px">
    <h3>Suggested Experiments</h3>
    <div id="res-experiments"><div class="no-data">No suggestions yet.</div></div>
  </div>

  <!-- Run history -->
  <div class="section" style="margin-top:14px">
    <h3>Research History <span style="color:#555;font-weight:normal;font-size:.65rem">(last 24 runs)</span></h3>
    <div id="res-history"><div class="no-data">No history yet.</div></div>
  </div>

  <!-- Strategy Proposals -->
  <div class="section" style="margin-top:14px">
    <h3>Strategy Proposals <span style="color:#555;font-weight:normal;font-size:.65rem">generated by research agent</span></h3>
    <div id="res-proposals"><div class="no-data">No proposals yet — generated automatically when a high-relevance strategy finding is found.</div></div>
  </div>

  <!-- Proposal code viewer modal -->
  <div id="proposal-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.85);z-index:1000;overflow:auto;padding:20px">
    <div style="max-width:800px;margin:0 auto;background:#1a1a1a;border-radius:8px;padding:20px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <h3 id="modal-title" style="margin:0"></h3>
        <button onclick="closeProposalModal()" style="background:none;border:none;color:#fff;font-size:1.2rem;cursor:pointer">&#x2715;</button>
      </div>
      <div id="modal-finding" style="font-size:.72rem;color:#888;margin-bottom:12px;padding:8px;background:#111;border-radius:4px"></div>
      <pre id="modal-code" style="background:#0a0a0a;padding:14px;border-radius:4px;overflow:auto;font-size:.7rem;color:#e0e0e0;max-height:60vh;white-space:pre-wrap"></pre>
      <div style="margin-top:14px;display:flex;gap:10px;align-items:center">
        <button id="modal-deploy-btn" onclick="deployProposal()" style="padding:8px 20px;background:#1565c0;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:700">Deploy Strategy</button>
        <span id="modal-deploy-status" style="font-size:.72rem;color:#555"></span>
      </div>
    </div>
  </div>
</div>

<div id="last-update">--</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
const $=id=>document.getElementById(id);
const fmt=(n,d=2)=>n==null?'--':'$'+Number(n).toFixed(d).replace(/\B(?=(\d{3})+(?!\d))/g,',');
const fmtPnl=n=>{if(n==null)return'--';const s=n>=0?'+':'-';return s+'$'+Math.abs(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g,',')};
const fmtN=n=>n==null?'--':Number(n).toFixed(4);
const ts=t=>new Date(t*1000).toLocaleTimeString();
const tsDate=t=>new Date(t*1000).toLocaleString();
const badge=s=>`<span class="badge ${s}">${s}</span>`;
const pnlClass=n=>n>=0?'green':'red';

let currentTab='overview';
let _statusInterval=null;

const allTabs=['overview','live','positions','trades','status','analytics','balances','meta','codereview','research'];

function showTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>{t.classList.toggle('active',allTabs[i]===name)});
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  $('tab-'+name).classList.add('active');
  currentTab=name;

  if(name==='status'){
    fetchSystemStatus();
    if(_statusInterval) clearInterval(_statusInterval);
    _statusInterval=setInterval(fetchSystemStatus,10000);
  } else {
    if(_statusInterval){clearInterval(_statusInterval);_statusInterval=null;}
  }

  if(name==='analytics'){
    fetchAnalytics();
  }

  if(name==='balances'){
    fetchBalances();
  }

  if(name==='codereview'){
    fetchCodeReview();
  }

  if(name==='research'){
    fetchResearch();
  }
}

const chartDefaults={responsive:true,maintainAspectRatio:true,plugins:{legend:{display:false}},scales:{x:{display:false,grid:{color:'#1a1a1a'}},y:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}}}};

const pnlCtx=$('pnlChart').getContext('2d');
const pnlChart=new Chart(pnlCtx,{type:'line',data:{labels:[],datasets:[{label:'Portfolio Value',data:[],borderColor:'#00e5ff',backgroundColor:'rgba(0,229,255,.05)',borderWidth:1.5,pointRadius:0,fill:true,tension:.3},{label:'Realized P&L',data:[],borderColor:'#00e676',backgroundColor:'transparent',borderWidth:1.5,pointRadius:0,tension:.3}]},options:{...chartDefaults,plugins:{legend:{display:true,labels:{color:'#666',font:{size:10},boxWidth:10}}}}});

const stratCtx=$('stratChart').getContext('2d');
const stratChart=new Chart(stratCtx,{type:'doughnut',data:{labels:[],datasets:[{data:[],backgroundColor:['#00e676','#7986cb','#ff7043','#ffd740','#4dd0e1','#ce93d8'],borderColor:'#0a0a0a',borderWidth:2}]},options:{responsive:true,maintainAspectRatio:true,plugins:{legend:{position:'right',labels:{color:'#666',font:{size:10},boxWidth:10}}}}});

// Analytics charts (lazy-init)
let roiChart=null,winRateChart=null,hourlyPnlChart=null,feeDragChart=null,healthTrendChart=null;

function getOrCreateChart(id,config){
  const ctx=$(id).getContext('2d');
  return new Chart(ctx,config);
}

function updatePnlChart(history){
  if(!history.length)return;
  const step=Math.max(1,Math.floor(history.length/150));
  const sampled=history.filter((_,i)=>i%step===0||i===history.length-1);
  pnlChart.data.labels=sampled.map(p=>ts(p.t));
  pnlChart.data.datasets[0].data=sampled.map(p=>p.value);
  pnlChart.data.datasets[1].data=sampled.map(p=>p.pnl);
  pnlChart.update('none');
}

function updateStratChart(counts){
  const entries=Object.entries(counts);
  stratChart.data.labels=entries.map(([k])=>k);
  stratChart.data.datasets[0].data=entries.map(([,v])=>v);
  stratChart.update('none');
}

async function fetchAll(){
  try{
    const [status,pnlH,stratPnl,stratTrades]=await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/pnl_history').then(r=>r.json()),
      fetch('/api/strategy_pnl').then(r=>r.json()),
      fetch('/api/strategy_trades').then(r=>r.json()),
    ]);
    updateStatus(status);
    updatePnlChart(pnlH);
    updateStratPnl(stratPnl);
    updateStratChart(stratTrades);

    if(currentTab==='positions'){
      const [open,closed]=await Promise.all([
        fetch('/api/positions').then(r=>r.json()),
        fetch('/api/closed_positions').then(r=>r.json()),
      ]);
      updatePositions(open,closed);
    }
    if(currentTab==='trades'){const d=await fetch('/api/trades').then(r=>r.json());updateTrades(d,status);}
    if(currentTab==='meta'){fetchMeta();}

    $('last-update').textContent='Updated: '+new Date().toLocaleTimeString();
  }catch(e){$('last-update').textContent='Connection error...';}
}

function updateStatus(s){
  $('uptime-info').textContent='Uptime: '+s.uptime+' | Cycles: '+s.cycle_count;
  $('balance').textContent=fmt(s.balance);

  const tv=s.total_value||0,tp=s.pnl||0,tpp=s.pnl_pct||0;
  $('total-value').textContent=fmt(tv);
  $('total-pnl-sub').textContent=(tp>=0?'+':'')+tp.toFixed(2)+' ('+tpp.toFixed(2)+'%)';
  $('total-pnl-sub').style.color=tp>=0?'#00e676':'#ff5252';

  const rp=s.realized_pnl||0,rpp=s.realized_pnl_pct||0;
  $('realized-pnl').textContent=fmtPnl(rp);
  $('realized-pnl').className='val '+(rp>=0?'green':'red');
  $('realized-pnl-pct').innerHTML=(rpp>=0?'+':'')+rpp.toFixed(2)+'% | <span id="closed-count">'+s.closed_positions+'</span> closed';

  const wr=s.win_rate||0;
  $('win-rate').textContent=wr.toFixed(1)+'%';
  $('win-rate').className='val '+(wr>=50?'green':'red');

  $('pos-count').textContent=s.open_positions;
  $('exposure-sub').textContent='Exposure: '+fmt(s.exposure);

  $('trades-per-hr').textContent=s.trades_per_hour||'--';
  $('total-trades-sub').textContent=(s.total_trades||0)+' total';

  $('fees').textContent=fmt(s.fees_paid);

  // live tab
  $('live-cycle').textContent=s.cycle_count;
  $('live-uptime').textContent=s.uptime;
  $('live-realized').textContent=fmtPnl(rp);
  $('live-realized').className='val '+(rp>=0?'green':'red');
  $('live-winrate').textContent=wr.toFixed(1)+'%';
  $('live-winrate').className='val '+(wr>=50?'green':'red');
  $('live-trades').textContent=s.total_trades;
}

function updateStratPnl(data){
  const entries=Object.entries(data);
  if(!entries.length){$('strat-bars').innerHTML='<div class="no-data">Waiting for trades...</div>';return;}
  const max=Math.max(...entries.map(([,v])=>Math.abs(v)),1);
  $('strat-bars').innerHTML=entries.sort((a,b)=>b[1]-a[1]).map(([name,val])=>{
    const pct=Math.abs(val)/max*100,cls=val>=0?'pos':'neg',sign=val>=0?'+':'';
    return`<div class="strat-row"><div class="name">${name}</div><div class="bar-wrap"><div class="bar ${cls}" style="width:${Math.max(pct,5)}%">${sign}$${val.toFixed(2)}</div></div></div>`;
  }).join('');
}

function updatePositions(open,closed){
  $('open-pos-count').textContent=open.length;
  if(!open.length){
    $('positions-table').innerHTML='<div class="no-data">No open positions</div>';
  }else{
    $('positions-table').innerHTML=`<table>
      <tr><th>Market</th><th>Outcome</th><th>Contracts</th><th>Avg Cost</th><th>Cost Basis</th><th>Strategy</th><th>Opened</th></tr>
      ${open.map(p=>`<tr>
        <td title="${p.question}">${p.question}</td>
        <td>${p.outcome}</td>
        <td>${p.contracts}</td>
        <td>${fmtN(p.avg_cost)}</td>
        <td>${fmt(p.cost_basis)}</td>
        <td>${badge(p.strategy)}</td>
        <td class="ts-small">${ts(p.opened_at)}</td>
      </tr>`).join('')}
    </table>`;
  }

  if(!closed.length){
    $('closed-table').innerHTML='<div class="no-data">No closed positions yet — positions close when fully sold</div>';
  }else{
    const totalR=closed.reduce((s,p)=>s+p.realized_pnl,0);
    const wins=closed.filter(p=>p.realized_pnl>0).length;
    $('closed-table').innerHTML=`
      <div style="display:flex;gap:20px;margin-bottom:10px;font-size:.78rem">
        <span>Total Realized: <strong class="${totalR>=0?'win':'loss'}">${fmtPnl(totalR)}</strong></span>
        <span>Win Rate: <strong class="${wins/closed.length>=.5?'win':'loss'}">${(wins/closed.length*100).toFixed(1)}%</strong></span>
        <span style="color:#555">(${wins}W / ${closed.length-wins}L of ${closed.length} closed)</span>
      </div>
      <table>
        <tr><th>Market</th><th>Outcome</th><th>Strategy</th><th>Realized P&L</th><th>Result</th><th>Closed</th><th>Duration</th></tr>
        ${closed.map(p=>{
          const dur=Math.round((p.closed_at-p.opened_at)/60);
          const durStr=dur<60?dur+'m':Math.round(dur/60)+'h '+dur%60+'m';
          const isWin=p.realized_pnl>0;
          return`<tr>
            <td title="${p.market_question||''}">${(p.market_question||'').slice(0,55)}</td>
            <td>${p.outcome||''}</td>
            <td>${badge(p.strategy)}</td>
            <td class="${isWin?'win':'loss'}">${fmtPnl(p.realized_pnl)}</td>
            <td><span style="color:${isWin?'#00e676':'#ff5252'};font-weight:700">${isWin?'WIN':'LOSS'}</span></td>
            <td class="ts-small">${ts(p.closed_at)}</td>
            <td class="ts-small">${durStr}</td>
          </tr>`;
        }).join('')}
      </table>`;
  }
}

function updateTrades(data,status){
  const buys=data.filter(t=>t.side==='BUY').length;
  $('t-total').textContent=status.total_trades;
  $('t-buys').textContent=buys;
  $('t-sells').textContent=data.length-buys;
  $('t-fees').textContent=fmt(status.fees_paid);
  if(!data.length){$('trades-table').innerHTML='<div class="no-data">No trades yet</div>';return;}
  $('trades-table').innerHTML=`<table>
    <tr><th>ID</th><th>Time</th><th>Strategy</th><th>Side</th><th>Contracts</th><th>Price</th><th>Amount</th><th>Notes</th></tr>
    ${data.map(t=>`<tr>
      <td>${t.trade_id}</td>
      <td class="ts-small">${ts(t.timestamp)}</td>
      <td>${badge(t.strategy)}</td>
      <td class="${t.side.toLowerCase()}">${t.side}</td>
      <td>${t.contracts}</td>
      <td>${fmtN(t.price)}</td>
      <td>${fmt(t.usdc_amount)}</td>
      <td style="color:#555">${t.notes}</td>
    </tr>`).join('')}
  </table>`;
}

// ------------------------------------------------------------------ //
//  Status tab                                                          //
// ------------------------------------------------------------------ //
async function fetchSystemStatus(){
  try{
    const d=await fetch('/api/system').then(r=>r.json());
    renderSystemStatus(d);
  }catch(e){
    $('status-connections').innerHTML='<div class="no-data">Failed to load system status</div>';
  }
}

function renderSystemStatus(d){
  // Mode badge in header
  const modeBadge=$('mode-badge');
  modeBadge.textContent=d.mode||'PAPER';
  modeBadge.style.background=d.mode==='LIVE'?'#3a1a1a':'#1a3a4a';
  modeBadge.style.color=d.mode==='LIVE'?'#ff5252':'#00e5ff';

  // Connections section
  const connItems=[
    {key:'polymarket',label:'Polymarket API'},
    {key:'binance',label:'Binance WebSocket'},
    {key:'kalshi',label:'Kalshi'},
  ];
  const apiItems=[
    {key:'anthropic',label:'Anthropic API'},
    {key:'polymarket',label:'Polymarket Key'},
    {key:'kalshi_rsa',label:'Kalshi RSA Key'},
    {key:'kalshi_token',label:'Kalshi Token'},
  ];

  let connHtml=connItems.map(({key,label})=>{
    const c=d.connections&&d.connections[key]||{status:'error',detail:''};
    return`<div class="status-item">
      <div class="dot ${c.status==='ok'?'ok':c.status==='warn'?'warn':'err'}"></div>
      <div><div class="status-label">${label}</div><div class="status-detail">${c.detail||''}</div></div>
    </div>`;
  }).join('');

  connHtml+=apiItems.map(({key,label})=>{
    const has=d.api_keys&&d.api_keys[key];
    return`<div class="status-item">
      <div class="dot ${has?'ok':'err'}"></div>
      <div><div class="status-label">${label}</div><div class="status-detail">${has?'Configured':'Not set'}</div></div>
    </div>`;
  }).join('');

  // Meta-agent connection item
  const ma=d.meta_agent||{};
  const maStatus=ma.enabled?'ok':'err';
  const maDetail=ma.enabled?(ma.last_run_ago_minutes!=null?'Last run '+ma.last_run_ago_minutes+'m ago':'Not run yet'):'No API key';
  connHtml+=`<div class="status-item">
    <div class="dot ${maStatus}"></div>
    <div><div class="status-label">Meta-Agent</div><div class="status-detail">${maDetail} · every ${ma.interval_minutes||30}m</div></div>
  </div>`;

  $('status-connections').innerHTML=connHtml;

  // Strategies section
  const strats=d.strategies||{};
  const stratHtml=Object.entries(strats).map(([name,info])=>{
    const cls=info.enabled?'enabled':'disabled';
    const statusTxt=info.enabled?'<span class="strat-status" style="color:#00e676">ENABLED</span>':'<span class="strat-status" style="color:#ff5252">DISABLED</span>';
    return`<div class="strat-card ${cls}">
      <h4>${name}</h4>
      ${statusTxt}
      <div class="strat-note">${info.note||''}</div>
    </div>`;
  }).join('');
  $('status-strategies').innerHTML=stratHtml||'<div class="no-data">No strategy info</div>';

  // Risk health section
  const risk=d.risk||{};
  const score=risk.health_score;
  const grade=risk.health_grade||'N/A';
  const gradeColor=grade==='HEALTHY'?'#00e676':grade==='WEAK'?'#ffd740':grade==='CRITICAL'?'#ff5252':'#888';
  const drawdown=risk.drawdown_pct||0;
  const exposure=risk.exposure_pct||0;
  const flags=risk.flags||[];
  const scoreDisplay=score!=null?score.toFixed(1):'--';
  const hardStopBadge=risk.hard_stop?'<span style="background:#3d0000;color:#ff5252;padding:2px 8px;border-radius:4px;font-size:.7rem;font-weight:700;margin-left:10px">HARD STOP</span>':'';

  $('status-risk').innerHTML=`
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:14px">
      <div style="font-size:2.5rem;font-weight:700;color:${gradeColor}">${scoreDisplay}</div>
      <div>
        <div style="font-size:1rem;font-weight:700;color:${gradeColor}">${grade}${hardStopBadge}</div>
        <div style="font-size:.7rem;color:#555;margin-top:4px">${flags.length?flags.join(' · '):'No active risk flags'}</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div>
        <div style="font-size:.65rem;color:#555;text-transform:uppercase;letter-spacing:.06em">Drawdown</div>
        <div style="font-size:.9rem;font-weight:700;color:${drawdown>10?'#ff5252':drawdown>5?'#ffd740':'#00e676'}">${drawdown.toFixed(2)}%</div>
        <div class="health-bar"><div class="health-fill" style="width:${Math.min(drawdown/15*100,100)}%;background:${drawdown>10?'#ff5252':drawdown>5?'#ffd740':'#00e676'}"></div></div>
      </div>
      <div>
        <div style="font-size:.65rem;color:#555;text-transform:uppercase;letter-spacing:.06em">Exposure</div>
        <div style="font-size:.9rem;font-weight:700;color:${exposure>80?'#ff5252':exposure>60?'#ffd740':'#00e676'}">${exposure.toFixed(1)}%</div>
        <div class="health-bar"><div class="health-fill" style="width:${Math.min(exposure,100)}%;background:${exposure>80?'#ff5252':exposure>60?'#ffd740':'#00e676'}"></div></div>
      </div>
    </div>`;

  // Disk section
  const disk=d.disk||{};
  $('status-disk').innerHTML=`
    <div style="display:flex;gap:30px;font-size:.85rem">
      <div><span style="color:#555;font-size:.65rem;text-transform:uppercase">Log Files</span><div style="font-weight:700;color:#00e5ff;margin-top:4px">${disk.log_files_count||0}</div></div>
      <div><span style="color:#555;font-size:.65rem;text-transform:uppercase">Total Size</span><div style="font-weight:700;color:#00e5ff;margin-top:4px">${(disk.log_files_mb||0).toFixed(2)} MB</div></div>
    </div>`;
}

// ------------------------------------------------------------------ //
//  Analytics tab                                                       //
// ------------------------------------------------------------------ //
async function fetchAnalytics(){
  try{
    const [d,status]=await Promise.all([
      fetch('/api/analytics').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
    ]);
    renderAnalytics(d);
    renderTimeToProfit(status,d);
  }catch(e){
    console.error('Analytics fetch failed',e);
  }
}

function fmtDuration(hours){
  if(hours==null||!isFinite(hours))return'--';
  if(hours<1)return Math.round(hours*60)+'m';
  if(hours<24)return hours.toFixed(1)+'h';
  const d=Math.floor(hours/24),h=Math.round(hours%24);
  return h>0?d+'d '+h+'h':d+'d';
}

function renderTimeToProfit(status,analytics){
  const closed=status.closed_positions||0;
  const uptimeH=(status.uptime_seconds||0)/3600;
  const realizedPnl=status.realized_pnl||0;
  const startBal=status.starting_balance||10000;

  // Close rate: positions closed per hour (needs >10min uptime to be meaningful)
  const closesPerHr=uptimeH>0.17?closed/uptimeH:null;

  // Hourly realized-PnL rate from last 6 data points in pnl_history (use analytics.hourly_pnl)
  const hourly=analytics.hourly_pnl||[];
  let hourlyRate=null;
  if(hourly.length>=2){
    const recent=hourly.slice(-6);
    const total=recent.reduce((s,h)=>s+h.pnl,0);
    hourlyRate=total/recent.length;
  }

  // Milestone definitions
  const BOOTSTRAP=30;   // need 30 closed positions to exit bootstrap
  const RELIABLE=50;    // 50 closed → win rate is statistically meaningful
  const CONFIDENT=100;  // 100 closed → strategy ROI is trustworthy

  const milestones=[
    {
      id:'bootstrap',
      label:'Exit Bootstrap Phase',
      sub:'30 closed positions → P&L data is meaningful',
      target:BOOTSTRAP,
      current:closed,
      hoursLeft:closed<BOOTSTRAP&&closesPerHr>0?(BOOTSTRAP-closed)/closesPerHr:null,
      done:closed>=BOOTSTRAP,
      color:'#ffd740',
    },
    {
      id:'reliable',
      label:'Reliable Win Rate',
      sub:'50 closed positions → win% stabilizes',
      target:RELIABLE,
      current:closed,
      hoursLeft:closed<RELIABLE&&closesPerHr>0?(RELIABLE-closed)/closesPerHr:null,
      done:closed>=RELIABLE,
      color:'#00e5ff',
    },
    {
      id:'confident',
      label:'Strategy Confidence',
      sub:'100 closed positions → trust per-strategy ROI',
      target:CONFIDENT,
      current:closed,
      hoursLeft:closed<CONFIDENT&&closesPerHr>0?(CONFIDENT-closed)/closesPerHr:null,
      done:closed>=CONFIDENT,
      color:'#7986cb',
    },
    {
      id:'breakeven',
      label:'Break-Even Realized P&L',
      sub:'Realized P&L turns positive',
      target:null,
      current:null,
      hoursLeft:realizedPnl<0&&hourlyRate>0?Math.abs(realizedPnl)/hourlyRate:null,
      done:realizedPnl>=0,
      color:'#00e676',
      isBreakeven:true,
    },
  ];

  // Phase badge
  let phase,phaseClass;
  if(closed>=CONFIDENT){phase='Strategy Proven';phaseClass='profit';}
  else if(closed>=RELIABLE){phase='Reliable Data';phaseClass='active';}
  else if(closed>=BOOTSTRAP){phase='Active Trading';phaseClass='active';}
  else{phase='Bootstrap Phase';phaseClass='bootstrap';}
  const badge=$('ttpl-phase-badge');
  badge.textContent=phase;
  badge.className='ttpl-badge '+phaseClass;

  // Render milestone cards
  $('ttpl-milestones').innerHTML=milestones.map(m=>{
    if(m.done){
      const label=m.isBreakeven
        ?'<span style="color:#00e676">+$'+realizedPnl.toFixed(2)+' realized</span>'
        :'<span style="color:#00e676">✓ Done ('+closed+' closed)</span>';
      return`<div class="ttpl-milestone done">
        <div class="ttpl-ms-label">${m.label}</div>
        <div class="ttpl-ms-eta">${label}</div>
        <div class="ttpl-ms-sub">${m.sub}</div>
        <div class="ttpl-ms-bar"><div class="ttpl-ms-fill" style="width:100%;background:${m.color}"></div></div>
      </div>`;
    }

    let etaText,pct,subLine;
    if(m.isBreakeven){
      if(realizedPnl>=0){
        etaText='In profit';pct=100;
      }else if(hourlyRate===null){
        etaText='Needs data';pct=0;
      }else if(hourlyRate<=0){
        etaText='Trending negative';pct=0;
      }else{
        etaText='~'+fmtDuration(m.hoursLeft);
        // progress toward $0 from starting low watermark
        const worst=Math.min(realizedPnl,-0.01);
        pct=Math.max(0,Math.min(99,(1-Math.abs(realizedPnl)/Math.abs(worst))*100));
      }
      subLine=hourlyRate!=null&&hourlyRate>0?'At $'+hourlyRate.toFixed(2)+'/hr current rate':m.sub;
    }else{
      if(closesPerHr===null){etaText='Needs data';pct=0;}
      else{
        etaText='~'+fmtDuration(m.hoursLeft);
        pct=Math.min(99,Math.max(0,(m.current/m.target)*100));
      }
      subLine=m.current+' / '+m.target+' closed'+(closesPerHr?` · ${closesPerHr.toFixed(1)}/hr`:'');
    }

    return`<div class="ttpl-milestone">
      <div class="ttpl-ms-label">${m.label}</div>
      <div class="ttpl-ms-eta">${etaText}</div>
      <div class="ttpl-ms-sub">${subLine}</div>
      <div class="ttpl-ms-bar"><div class="ttpl-ms-fill" style="width:${pct}%;background:${m.color}"></div></div>
    </div>`;
  }).join('');

  // Verdict text
  let verdict='';
  if(closed<BOOTSTRAP){
    const h=closesPerHr>0?fmtDuration((BOOTSTRAP-closed)/closesPerHr):'unknown time';
    verdict=`You're in the <strong style="color:#ffd740">bootstrap phase</strong> (${closed}/${BOOTSTRAP} closed positions). `
      +`P&L numbers exist but aren't statistically reliable yet. Estimated <strong>${h}</strong> until the data is meaningful. `
      +`The meta-agent won't auto-tune parameters until bootstrap exits.`;
  }else if(closed<RELIABLE){
    verdict=`Bootstrap cleared! Win rate and ROI numbers are forming but need ${RELIABLE-closed} more closed positions to stabilize. `
      +`Watch the Strategy ROI chart — strategies with negative ROI after ${RELIABLE} trades are candidates to disable.`;
  }else if(realizedPnl<0){
    const etaStr=hourlyRate>0?'~'+fmtDuration(Math.abs(realizedPnl)/hourlyRate)+' at current pace':'unclear (needs more recent trade data)';
    verdict=`Data is reliable (${closed} closed positions). Realized P&L is currently <strong style="color:#ff5252">$${realizedPnl.toFixed(2)}</strong>. `
      +`Break-even estimated in <strong>${etaStr}</strong>. Focus on strategies with positive ROI and disable outliers.`;
  }else{
    verdict=`<strong style="color:#00e676">You're in profit.</strong> Realized P&L: <strong>+$${realizedPnl.toFixed(2)}</strong> across ${closed} closed positions. `
      +`Win rate and strategy ROI data below reflect real performance.`;
  }
  $('ttpl-verdict').innerHTML=verdict;
}

function hbar(labels,values,colors){
  return{
    type:'bar',
    data:{
      labels,
      datasets:[{data:values,backgroundColor:colors,borderColor:'transparent',borderWidth:0}]
    },
    options:{
      indexAxis:'y',
      responsive:true,
      maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{
        x:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}},
        y:{grid:{color:'#1a1a1a'},ticks:{color:'#aaa',font:{size:10}}}
      }
    }
  };
}

function renderAnalytics(d){
  // Strategy ROI chart
  const roiEntries=Object.entries(d.strategy_roi||{});
  if(roiEntries.length){
    const labels=roiEntries.map(([k])=>k);
    const values=roiEntries.map(([,v])=>v);
    const colors=values.map(v=>v>=0?'rgba(0,230,118,.6)':'rgba(255,82,82,.6)');
    if(roiChart){roiChart.destroy();}
    roiChart=new Chart($('roiChart').getContext('2d'),hbar(labels,values,colors));
  }

  // Win rate chart
  const wrEntries=Object.entries(d.strategy_win_rates||{});
  if(wrEntries.length){
    const labels=wrEntries.map(([k])=>k);
    const values=wrEntries.map(([,v])=>v);
    const colors=values.map(v=>v>=50?'rgba(0,229,255,.6)':'rgba(255,215,64,.6)');
    if(winRateChart){winRateChart.destroy();}
    winRateChart=new Chart($('winRateChart').getContext('2d'),hbar(labels,values,colors));
  }

  // Hourly PnL chart
  const hourly=d.hourly_pnl||[];
  if(hourly.length){
    if(hourlyPnlChart){hourlyPnlChart.destroy();}
    hourlyPnlChart=new Chart($('hourlyPnlChart').getContext('2d'),{
      type:'bar',
      data:{
        labels:hourly.map(h=>h.hour_label),
        datasets:[{
          data:hourly.map(h=>h.pnl),
          backgroundColor:hourly.map(h=>h.pnl>=0?'rgba(0,230,118,.6)':'rgba(255,82,82,.6)'),
          borderWidth:0
        }]
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{x:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}},y:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}}}
      }
    });
  }

  // Fee drag chart
  const feeEntries=Object.entries(d.strategy_fees||{});
  if(feeEntries.length){
    const labels=feeEntries.map(([k])=>k);
    const values=feeEntries.map(([,v])=>v);
    if(feeDragChart){feeDragChart.destroy();}
    feeDragChart=new Chart($('feeDragChart').getContext('2d'),hbar(labels,values,values.map(()=>'rgba(255,112,67,.6)')));
  }

  // Health trend chart
  const hh=d.health_history||[];
  if(hh.length){
    if(healthTrendChart){healthTrendChart.destroy();}
    healthTrendChart=new Chart($('healthTrendChart').getContext('2d'),{
      type:'line',
      data:{
        labels:hh.map(h=>new Date(h.t*1000).toLocaleString()),
        datasets:[{
          label:'Health Score',
          data:hh.map(h=>h.score),
          borderColor:'#7986cb',
          backgroundColor:'rgba(121,134,203,.1)',
          borderWidth:1.5,
          pointRadius:3,
          fill:true,
          tension:.3
        }]
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{
          x:{display:false,grid:{color:'#1a1a1a'}},
          y:{min:0,max:100,grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}}
        }
      }
    });
  }else{
    $('healthTrendChart').closest('.section').querySelector('h3').insertAdjacentHTML('afterend','<div class="no-data" style="padding:20px">No health history yet — run meta-agent first</div>');
  }

  // LLM decisions table
  const llm=d.llm_decisions||[];
  const now=Date.now()/1000;
  if(llm.length){
    $('llm-decisions-table').innerHTML=`<table>
      <tr><th>Time</th><th>Strategy</th><th>Side</th><th>Price</th><th>Amount</th><th>Notes</th></tr>
      ${llm.map(t=>`<tr>
        <td class="ts-small">${ts(t.timestamp)}</td>
        <td>${badge(t.strategy)}</td>
        <td class="${t.side.toLowerCase()}">${t.side}</td>
        <td>${fmtN(t.price)}</td>
        <td>${fmt(t.usdc_amount)}</td>
        <td style="color:#7986cb;font-size:.68rem">${t.notes}</td>
      </tr>`).join('')}
    </table>`;
  }

  // Active signals: LLM trades in last hour
  const active=llm.filter(t=>now-t.timestamp<3600);
  if(active.length){
    $('llm-active-signals').innerHTML=`<div style="display:flex;flex-wrap:wrap;gap:8px">${active.map(t=>`
      <div style="background:#1a1a2a;border:1px solid #7986cb;border-radius:6px;padding:8px 12px;font-size:.72rem">
        <span class="${t.side.toLowerCase()}">${t.side}</span> via ${badge(t.strategy)} · ${fmtN(t.price)} · ${fmt(t.usdc_amount)}
        <div style="color:#555;margin-top:3px">${t.notes.substring(0,80)}</div>
      </div>`).join('')}</div>`;
  }else{
    $('llm-active-signals').innerHTML='<div class="no-data">None in last hour</div>';
  }

  // Parameter change timeline from meta history
  fetch('/api/meta/history').then(r=>r.json()).then(hist=>{
    const changes=[];
    hist.forEach(h=>{
      const applied=h.applied_changes||[];
      const proposed=h.proposed_changes||{};
      if(applied.length){
        changes.push({
          t:h.timestamp,
          keys:applied,
          proposed
        });
      }
    });
    if(changes.length){
      $('param-timeline').innerHTML=`<div style="display:flex;flex-direction:column;gap:8px">${changes.map(c=>`
        <div style="display:flex;gap:12px;align-items:flex-start">
          <div class="ts-small" style="white-space:nowrap;min-width:120px;margin-top:2px">${tsDate(c.t)}</div>
          <div>${c.keys.map(k=>`<span style="background:#1a2a1a;color:#00e676;padding:1px 7px;border-radius:3px;font-size:.65rem;margin-right:4px">${k} → ${c.proposed[k]||'?'}</span>`).join('')}</div>
        </div>`).join('')}
      </div>`;
    }else{
      $('param-timeline').innerHTML='<div class="no-data">No parameter changes applied yet</div>';
    }
  }).catch(()=>{});
}

// ------------------------------------------------------------------ //
//  Balances tab                                                        //
// ------------------------------------------------------------------ //
async function fetchBalances(){
  try{
    const d=await fetch('/api/balances').then(r=>r.json());
    renderBalances(d);
  }catch(e){
    console.error('Balances fetch failed',e);
  }
}

function fmtUsd(n){
  if(n==null||n===undefined)return'--';
  return'$'+Number(n).toFixed(2);
}

function renderBalances(d){
  const cy=d.billing_cycle||{};
  const ant=d.anthropic||{};
  const rail=d.railway||{};
  const bot=d.bot||{};

  // Billing cycle bar
  const pct=cy.cycle_pct||0;
  $('cycle-fill').style.width=pct+'%';
  $('cycle-pct').textContent=pct.toFixed(1);
  $('cycle-start').textContent=cy.start||'--';
  $('cycle-end').textContent=cy.end||'--';
  $('cycle-days-left').textContent=(cy.days_remaining||0)+' days remaining';

  // Anthropic
  $('bal-ant-model').textContent=ant.model||'--';
  $('bal-ant-key').innerHTML=ant.key_configured
    ?'<span style="color:#00e676">Configured</span>'
    :'<span style="color:#ff5252">Not set</span>';
  $('bal-ant-runs').textContent=ant.meta_agent_runs||0;
  $('bal-ant-cpr').textContent=fmtUsd(ant.cost_per_run_usd);
  $('bal-ant-cost').textContent=fmtUsd(ant.estimated_cost_usd);
  $('bal-ant-proj').textContent=fmtUsd(ant.projected_monthly_usd);
  if(ant.monthly_budget_usd){
    $('bal-ant-budget').textContent=fmtUsd(ant.monthly_budget_usd);
    const usedPct=Math.min((ant.estimated_cost_usd/ant.monthly_budget_usd)*100,100);
    $('bal-ant-bar').style.width=usedPct+'%';
    $('bal-ant-bar').style.background=usedPct>80?'#ff5252':usedPct>60?'#ffd740':'#ce93d8';
    $('bal-ant-bar-lbl').textContent=fmtUsd(ant.estimated_cost_usd)+' used of '+fmtUsd(ant.monthly_budget_usd);
    $('bal-ant-bar-pct').textContent=usedPct.toFixed(1)+'%';
  }else{
    $('bal-ant-bar').style.width='0%';
    const projPct=ant.projected_monthly_usd||0;
    $('bal-ant-bar-lbl').textContent='Projected this month: '+fmtUsd(projPct);
  }

  // Railway
  $('bal-rail-base').textContent=fmtUsd(rail.plan_base_cost_usd)+'/mo';
  $('bal-rail-days').textContent=(rail.days_remaining_in_cycle||0)+' days';
  const diskMb=rail.disk_used_mb||0;
  const diskLim=rail.disk_limit_mb||512;
  $('bal-rail-disk').textContent=diskMb.toFixed(2)+' MB / '+diskLim+' MB';
  const diskPct=rail.disk_pct||0;
  $('bal-rail-bar').style.width=Math.min(diskPct,100)+'%';
  $('bal-rail-bar').style.background=diskPct>80?'#ff5252':diskPct>60?'#ffd740':'#4dd0e1';
  $('bal-rail-bar-pct').textContent=diskPct.toFixed(1)+'%';

  // Bot
  $('bal-bot-mode').innerHTML=bot.paper_trading
    ?'<span style="color:#00e5ff">PAPER</span>'
    :'<span style="color:#ff5252">LIVE</span>';
  const uh=bot.uptime_hours||0;
  const uptimeStr=uh>=24?Math.floor(uh/24)+'d '+Math.round(uh%24)+'h':uh.toFixed(1)+'h';
  $('bal-bot-uptime').textContent=uptimeStr;
  $('bal-bot-trades').textContent=bot.trades_executed||0;
  const pnl=bot.total_pnl_usd||0;
  $('bal-bot-pnl').innerHTML='<span style="color:'+(pnl>=0?'#00e676':'#ff5252')+'">'+fmtUsd(pnl)+'</span>';
  // Cycles left: 30-min meta-agent cadence × days remaining × 48 runs/day
  const metaRunsPerDay=48;
  const botDaysLeft=cy.days_remaining||0;
  $('bal-bot-cycles').textContent=Math.round(botDaysLeft*24*2)+' scan cycles';
  $('bal-bot-metaruns').textContent=Math.round(botDaysLeft*metaRunsPerDay)+' runs';
}

// ------------------------------------------------------------------ //
//  Research tab                                                        //
// ------------------------------------------------------------------ //
async function fetchResearch(){
  try{
    const [latest,history,signals,proposals]=await Promise.all([
      fetch('/api/research/latest').then(r=>r.json()),
      fetch('/api/research/list').then(r=>r.json()),
      fetch('/api/research/signals').then(r=>r.json()),
      fetch('/api/research/proposals/list').then(r=>r.json()),
    ]);
    renderResearch(latest,history);
    renderResearchSignals(signals);
    renderProposals(proposals);
  }catch(e){
    console.error('Research fetch failed',e);
  }
}

function renderResearch(d,history){
  const intervalH=parseFloat(localStorage.getItem('researchInterval')||'2');

  if(!d||!d.found){
    $('res-last').textContent='Never';
    $('res-next').textContent='soon';
    $('res-total').textContent='--';
    $('res-high').textContent='--';
    $('res-websearch').textContent='--';
    $('res-interval').textContent='every '+intervalH+'h';
    return;
  }

  // Header cards
  $('res-last').textContent=(d.date||'')+(d.run_hour?' '+d.run_hour:'');
  const nextTs=(d.timestamp||0)+intervalH*3600;
  const diffM=Math.round((nextTs-Date.now()/1000)/60);
  $('res-next').textContent=diffM>0?'in ~'+diffM+'m':'soon';
  $('res-total').textContent=(d.finding_count||0)+' total';
  $('res-high').innerHTML='<span style="color:#00e676">'+(d.high_count||0)+' high</span> · <span style="color:#ffd740">'+(d.medium_count||0)+' medium</span>';
  const ws=d.web_search_used;
  $('res-websearch').innerHTML=ws
    ?'<span style="color:#00e676">Live</span>'
    :'<span style="color:#ffd740">Training data</span>';
  $('res-interval').textContent='every '+intervalH+'h';
  $('res-run-label').textContent='Run #'+(d.run_index!=null?d.run_index:'?')+' · '+((d.topics_searched||[]).length)+' topics searched';

  // Top insights
  const insights=d.top_insights||[];
  if(insights.length){
    $('res-insights').innerHTML=insights.map(ins=>`
      <div class="res-insight">
        <span class="res-insight-bullet">◆</span>
        <span>${ins}</span>
      </div>`).join('');
  }else{
    $('res-insights').innerHTML='<div class="no-data">No top insights extracted.</div>';
  }

  // Topics label
  const topics=d.topics_searched||[];
  if(topics.length){
    $('res-topics-label').textContent='— searched: '+topics.join(' · ');
  }

  // Findings
  const findings=d.findings||[];
  const relOrder={high:0,medium:1,low:2};
  const sorted=[...findings].sort((a,b)=>(relOrder[a.relevance]||9)-(relOrder[b.relevance]||9));
  if(sorted.length){
    $('res-findings').innerHTML=sorted.map(f=>`
      <div class="res-finding ${f.relevance||'low'}">
        <div class="res-finding-meta">
          <span class="res-rel ${f.relevance||'low'}">${f.relevance||'low'}</span>
          <span class="res-cat">${f.category||''}</span>
          <span class="res-source">${f.source||''}</span>
        </div>
        <div class="res-title">${f.title||''}</div>
        <div class="res-summary">${f.summary||''}</div>
        ${f.actionable_suggestion?`<div class="res-suggestion">→ ${f.actionable_suggestion}</div>`:''}
      </div>`).join('');
  }else{
    $('res-findings').innerHTML='<div class="no-data">No findings in this run.</div>';
  }

  // Suggested experiments
  const exps=d.suggested_experiments||[];
  if(exps.length){
    $('res-experiments').innerHTML=exps.map((e,i)=>`
      <div class="res-experiment">
        <span class="res-exp-num">${i+1}.</span>
        <span>${e}</span>
      </div>`).join('');
  }else{
    $('res-experiments').innerHTML='<div class="no-data">No experiments suggested.</div>';
  }

  // History table
  if(history&&history.length){
    $('res-history').innerHTML=`<table>
      <tr><th>Date</th><th>Time</th><th>Findings</th><th>Web</th><th>Topics</th><th>Top Insight</th></tr>
      ${history.map(r=>`<tr>
        <td class="ts-small">${r.date||'--'}</td>
        <td class="ts-small">${r.run_hour||'--'}</td>
        <td><span style="color:#00e676">${r.high_count||0}H</span> / ${r.finding_count||0} total</td>
        <td>${r.web_search_used?'<span style="color:#00e676">✓</span>':'<span style="color:#555">✗</span>'}</td>
        <td style="color:#555;font-size:.65rem">${(r.topics_searched||[]).slice(0,2).map(t=>t.split(' ').slice(0,3).join(' ')).join(', ')}</td>
        <td style="color:#888;font-size:.68rem">${(r.top_insights||[])[0]||'--'}</td>
      </tr>`).join('')}
    </table>`;
  }
}

// ------------------------------------------------------------------ //
//  Code Review tab                                                     //
// ------------------------------------------------------------------ //
async function fetchCodeReview(){
  try{
    const [latest,history]=await Promise.all([
      fetch('/api/code_review/latest').then(r=>r.json()),
      fetch('/api/code_review/list').then(r=>r.json()),
    ]);
    renderCodeReview(latest,history);
  }catch(e){
    console.error('Code review fetch failed',e);
  }
}

function renderCodeReview(d,history){
  if(!d||!d.found){
    $('cr-grade').textContent='--';
    $('cr-score').textContent='--';
    $('cr-date').textContent='Never';
    $('cr-total').textContent='--';
    $('cr-severity').textContent='runs weekly';
    return;
  }

  const grade=d.grade||'?';
  const gradeEl=$('cr-grade');
  gradeEl.textContent=grade;
  gradeEl.className='val cr-grade-'+grade;

  const score=d.health_score;
  const scoreEl=$('cr-score');
  scoreEl.textContent=score!=null?score:'--';
  scoreEl.className='val '+(score>=75?'green':score>=50?'yellow':'red');

  $('cr-date').textContent=d.date||'--';
  $('cr-total').textContent=(d.total_findings||0)+' total';
  $('cr-severity').textContent=(d.high_findings||0)+' high · '+(d.medium_findings||0)+' medium · '+(d.low_findings||0)+' low';

  // Summary
  $('cr-summary').textContent=d.summary||'No summary.';

  // Strengths
  const strengths=d.strengths||[];
  if(strengths.length){
    $('cr-strengths-card').style.display='';
    $('cr-strengths').innerHTML=strengths.map(s=>`<div class="cr-strength"><span style="color:#00e676">✓</span>${s}</div>`).join('');
  }

  // Findings
  const findings=d.findings||[];
  if(!findings.length){
    $('cr-findings').innerHTML='<div class="no-data">No findings — code looks clean!</div>';
  }else{
    const sevOrder={high:0,medium:1,low:2,info:3};
    const sorted=[...findings].sort((a,b)=>(sevOrder[a.severity]||9)-(sevOrder[b.severity]||9));
    $('cr-findings').innerHTML=sorted.map(f=>`
      <div class="cr-finding ${f.severity||'info'}">
        <div class="cr-finding-header">
          <span class="cr-sev ${f.severity||'info'}">${f.severity||'info'}</span>
          <span class="cr-cat">${f.category||''}</span>
          <span class="cr-file">${f.file||''}</span>
        </div>
        <div class="cr-title">${f.title||''}</div>
        <div class="cr-desc">${f.description||''}</div>
        ${f.suggestion?`<div class="cr-suggestion">💡 ${f.suggestion}</div>`:''}
      </div>`).join('');
  }

  // History table
  if(history&&history.length){
    $('cr-history').innerHTML=`<table>
      <tr><th>Date</th><th>Grade</th><th>Score</th><th>Findings</th><th>Summary</th></tr>
      ${history.map(r=>`<tr>
        <td class="ts-small">${r.date||'--'}</td>
        <td class="cr-grade-${r.grade||'?'}" style="font-weight:700">${r.grade||'?'}</td>
        <td>${r.health_score!=null?r.health_score:'--'}</td>
        <td>${r.high_findings||0}H / ${r.medium_findings||0}M / ${(r.total_findings||0)-(r.high_findings||0)-(r.medium_findings||0)}L</td>
        <td style="color:#555;font-size:.7rem">${r.summary||''}</td>
      </tr>`).join('')}
    </table>`;
  }
}

async function fetchMeta(){
  const [hist,latest]=await Promise.all([
    fetch('/api/meta/history').then(r=>r.json()),
    fetch('/api/meta/latest').then(r=>r.json()),
  ]);
  $('meta-count').textContent=hist.length;
  $('meta-last').textContent=hist.length?tsDate(hist[0].timestamp):'Never';
  if(hist.length){
    const nextTs=(hist[0].timestamp||0)+1800;
    const diff=Math.round((nextTs-Date.now()/1000)/60);
    $('meta-next').textContent=diff>0?'in ~'+diff+'m':'soon';
  }

  if(latest.found){
    const ch=latest.proposed_changes||{};
    const rows=Object.entries(ch).map(([k,v])=>`<tr><td>${k}</td><td>${latest.current_values?.[k]||'?'}</td><td>${v}</td></tr>`).join('');
    $('meta-latest-card').innerHTML=`
      <div class="meta-card">
        <h3>Latest Analysis — ${tsDate(latest.timestamp)}</h3>
        <div class="meta-analysis">${latest.analysis||''}</div>
        ${rows?`<br><table class="change-table"><tr><th>Parameter</th><th>Was</th><th>Proposed</th></tr>${rows}</table>`:'<p style="color:#555;margin-top:8px;font-size:.75rem">No parameter changes suggested.</p>'}
      </div>`;
  }

  if(hist.length){
    $('meta-history').innerHTML=`<table>
      <tr><th>Time</th><th>Portfolio P&L</th><th>Changes Suggested</th><th>Preview</th></tr>
      ${hist.map(h=>`<tr>
        <td class="ts-small">${tsDate(h.timestamp)}</td>
        <td class="${h.portfolio_pnl>=0?'buy':'sell'}">${fmtPnl(h.portfolio_pnl)}</td>
        <td>${Object.keys(h.proposed_changes||{}).length} suggested / <span class="win">${(h.applied_changes||[]).length} applied</span></td>
        <td style="color:#555">${h.analysis_preview}</td>
      </tr>`).join('')}
    </table>`;
  }else{
    $('meta-history').innerHTML='<div class="no-data">No analyses yet.</div>';
  }
}

const evtSource=new EventSource('/api/logs/stream');
evtSource.onmessage=e=>{
  const entry=JSON.parse(e.data);
  const feed=$('log-feed');
  const d=document.createElement('div');
  d.className='log-line';
  const t=new Date(entry.t*1000).toLocaleTimeString('en',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:2});
  d.innerHTML=`<span class="ts">${t}</span><span class="lvl ${entry.level}">${entry.level.substring(0,4)}</span>${entry.msg}`;
  feed.appendChild(d);
  if($('autoscroll').checked)feed.scrollTop=feed.scrollHeight;
  while(feed.children.length>500)feed.removeChild(feed.firstChild);
};

fetch('/api/logs?limit=200').then(r=>r.json()).then(logs=>{
  const feed=$('log-feed');
  logs.forEach(entry=>{
    const d=document.createElement('div');
    d.className='log-line';
    const t=new Date(entry.t*1000).toLocaleTimeString('en',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
    d.innerHTML=`<span class="ts">${t}</span><span class="lvl ${entry.level}">${entry.level.substring(0,4)}</span>${entry.msg}`;
    feed.appendChild(d);
  });
  feed.scrollTop=feed.scrollHeight;
});

async function resetPortfolio(){
  if(!confirm('Reset portfolio to $10,000? This will erase all trades, positions, and history.'))return;
  try{
    const r=await fetch('/api/reset',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      alert('Portfolio reset to $'+d.starting_balance.toLocaleString());
      fetchAll();
    }else{
      alert('Reset failed: '+(d.error||'unknown error'));
    }
  }catch(e){alert('Reset failed: '+e);}
}

// ------------------------------------------------------------------ //
//  Auto-fix                                                           //
// ------------------------------------------------------------------ //
let _autofixPolling=null;

async function triggerAutofix(){
  const btn=$('cr-autofix-btn');
  btn.disabled=true;
  btn.textContent='Running…';
  $('cr-autofix-status').textContent='Starting…';
  $('cr-autofix-results').innerHTML='';
  try{
    const r=await fetch('/api/code_review/autofix',{method:'POST'});
    const d=await r.json();
    if(!d.ok){
      $('cr-autofix-status').textContent='Error: '+(d.error||'unknown');
      btn.disabled=false;btn.textContent='▶ Run Auto-Fix';
      return;
    }
  }catch(e){
    $('cr-autofix-status').textContent='Request failed: '+e;
    btn.disabled=false;btn.textContent='▶ Run Auto-Fix';
    return;
  }
  // Start polling
  if(_autofixPolling)clearInterval(_autofixPolling);
  _autofixPolling=setInterval(pollAutofix,2000);
}

async function pollAutofix(){
  try{
    const d=await fetch('/api/code_review/autofix/status').then(r=>r.json());
    renderAutofixStatus(d);
    if(d.state!=='running'){
      clearInterval(_autofixPolling);_autofixPolling=null;
      const btn=$('cr-autofix-btn');
      btn.disabled=false;btn.textContent='▶ Run Auto-Fix';
    }
  }catch(e){console.error('autofix poll error',e);}
}

function renderAutofixStatus(d){
  const statusEl=$('cr-autofix-status');
  if(d.state==='running'){
    const n=d.results?d.results.length:0;
    statusEl.textContent=`Running… (${n} processed so far)`;
    statusEl.style.color='#ffd740';
  }else if(d.state==='done'){
    const fixed=(d.results||[]).filter(r=>r.status==='fixed').length;
    const total=(d.results||[]).length;
    const git=d.git||{};
    const gitMsg=git.pushed?` ✅ Pushed to GitHub — Railway redeploying`
      :git.message?` ⚠️ ${git.message}`:` (no git push)`;
    statusEl.innerHTML=`Done — ${fixed}/${total} fixes applied.<br><span style="font-size:.7rem;color:${git.pushed?'#00e676':'#ffd740'}">${gitMsg}</span>`;
    statusEl.style.color='#00e676';
  }else if(d.state==='error'){
    statusEl.textContent='Error: '+(d.error||'unknown');
    statusEl.style.color='#ff5252';
  }else{
    statusEl.textContent='';
  }

  const results=d.results||[];
  if(!results.length){
    $('cr-autofix-results').innerHTML='';
    return;
  }

  const statusIcon={fixed:'✅',skip:'⏭',error:'❌',pending:'⏳',info:'ℹ️'};
  const statusColor={fixed:'#00e676',skip:'#555',error:'#ff5252',pending:'#ffd740',info:'#90caf9'};

  $('cr-autofix-results').innerHTML=`<div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">`+
    results.map(r=>`
      <div style="display:flex;gap:10px;align-items:flex-start;padding:7px 10px;background:#111;border-radius:4px;border-left:3px solid ${statusColor[r.status]||'#555'}">
        <span style="font-size:.9rem;min-width:20px">${statusIcon[r.status]||'•'}</span>
        <div style="flex:1;min-width:0">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <span style="font-size:.68rem;font-weight:700;color:${statusColor[r.status]||'#555'}">${(r.status||'').toUpperCase()}</span>
            <span style="font-size:.68rem;color:#ffd740">${r.severity||''}</span>
            <span style="font-size:.68rem;color:#888;font-family:monospace">${r.file||''}</span>
          </div>
          <div style="font-size:.72rem;font-weight:600;margin-top:2px">${r.title||''}</div>
          <div style="font-size:.68rem;color:#888;margin-top:2px">${r.message||''}</div>
        </div>
      </div>`).join('')+
    `</div>`;
}

function renderResearchSignals(s){
  if(!s||!s.active_topics){return;}
  const topics=s.active_topics||[];
  $('res-active-topics').innerHTML=topics.length
    ? topics.map(t=>`<span style="background:#1a2744;color:#90caf9;padding:2px 8px;border-radius:10px;font-size:.65rem">${t}</span>`).join('')
    : '<span style="color:#555;font-size:.68rem">none</span>';
  $('res-signal-focus').textContent=s.strategy_focus||'none';
  $('res-signal-confidence').textContent=s.confidence||'--';
  const hints=s.param_hints||{};
  const hintStr=Object.keys(hints).length
    ? Object.entries(hints).map(([k,v])=>`${k}=${v}`).join(', ')
    : 'none';
  $('res-signal-params').textContent=hintStr;
}

let _currentProposalId=null;

function renderProposals(proposals){
  if(!proposals||!proposals.length){
    $('res-proposals').innerHTML='<div class="no-data">No proposals yet.</div>';
    return;
  }
  $('res-proposals').innerHTML=proposals.map(p=>`
    <div style="padding:10px 14px;background:#111;border-radius:6px;margin-bottom:8px;display:flex;gap:14px;align-items:flex-start">
      <div style="flex:1;min-width:0">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
          <span style="font-size:.72rem;font-weight:700;color:#90caf9">${p.class_name||'?'}</span>
          ${p.deployed?'<span style="font-size:.65rem;background:#1b5e20;color:#00e676;padding:1px 7px;border-radius:8px">deployed</span>':'<span style="font-size:.65rem;background:#1a237e;color:#90caf9;padding:1px 7px;border-radius:8px">proposed</span>'}
        </div>
        <div style="font-size:.72rem;font-weight:600;margin-bottom:2px">${p.finding_title||''}</div>
        <div style="font-size:.68rem;color:#888">${(p.finding_summary||'').substring(0,180)}${(p.finding_summary||'').length>180?'\u2026':''}</div>
      </div>
      <button onclick="viewProposal('${p.id}')" style="padding:5px 12px;background:#1a237e;color:#90caf9;border:none;border-radius:4px;cursor:pointer;font-size:.7rem;white-space:nowrap">View Code</button>
    </div>`).join('');
}

async function viewProposal(id){
  try{
    const d=await fetch('/api/research/proposals/'+id).then(r=>r.json());
    if(d.error){alert(d.error);return;}
    _currentProposalId=id;
    $('modal-title').textContent=d.class_name||'Strategy Proposal';
    $('modal-finding').textContent=d.finding_title+' \u2014 '+d.finding_summary;
    $('modal-code').textContent=d.code||'(no code)';
    $('modal-deploy-btn').disabled=!!d.deployed;
    $('modal-deploy-btn').textContent=d.deployed?'Already Deployed':'Deploy Strategy';
    $('modal-deploy-status').textContent=d.deployed?'Deployed \u2014 restart bot to activate':'';
    $('proposal-modal').style.display='block';
  }catch(e){alert('Failed to load proposal: '+e);}
}

function closeProposalModal(){
  $('proposal-modal').style.display='none';
  _currentProposalId=null;
}

async function deployProposal(){
  if(!_currentProposalId)return;
  if(!confirm('Deploy this strategy? It will be copied to src/strategies/ and hot-loaded into the running bot.'))return;
  const btn=$('modal-deploy-btn');
  btn.disabled=true;btn.textContent='Deploying\u2026';
  $('modal-deploy-status').textContent='';
  try{
    const r=await fetch('/api/research/proposals/'+_currentProposalId+'/deploy',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      $('modal-deploy-status').textContent='\u2705 Deployed to '+d.deployed_path+' \u2014 hot-loading\u2026';
      $('modal-deploy-status').style.color='#00e676';
      btn.textContent='Deployed';
    }else{
      $('modal-deploy-status').textContent='\u274c '+(d.error||'Unknown error');
      $('modal-deploy-status').style.color='#ff5252';
      btn.disabled=false;btn.textContent='Deploy Strategy';
    }
  }catch(e){
    $('modal-deploy-status').textContent='\u274c '+e;
    $('modal-deploy-status').style.color='#ff5252';
    btn.disabled=false;btn.textContent='Deploy Strategy';
  }
}

// ------------------------------------------------------------------ //
//  Agent countdown timers                                             //
// ------------------------------------------------------------------ //
let _timerData = null;
let _timerFetchedAt = 0;

function fmtCountdown(secs){
  if(secs==null)return'never run';
  if(secs<=0)return'<span style="color:#00e676">running soon</span>';
  const h=Math.floor(secs/3600);
  const m=Math.floor((secs%3600)/60);
  const s=Math.floor(secs%60);
  if(h>0)return h+'h '+String(m).padStart(2,'0')+'m';
  if(m>0)return m+'m '+String(s).padStart(2,'0')+'s';
  return String(s)+'s';
}

function fmtLastRun(ts){
  if(!ts)return'never';
  const diff=Math.round((Date.now()/1000)-ts);
  if(diff<60)return diff+'s ago';
  if(diff<3600)return Math.floor(diff/60)+'m ago';
  if(diff<86400)return Math.floor(diff/3600)+'h ago';
  return Math.floor(diff/86400)+'d ago';
}

function tickTimers(){
  if(!_timerData)return;
  const elapsed=(Date.now()/1000)-_timerFetchedAt;
  const agents=[
    {key:'meta_agent',   timerId:'timer-meta',     subId:'timer-meta-sub'},
    {key:'research',     timerId:'timer-research',  subId:'timer-research-sub'},
    {key:'code_review',  timerId:'timer-review',    subId:'timer-review-sub'},
  ];
  for(const {key,timerId,subId} of agents){
    const d=_timerData[key];
    if(!d)continue;
    const nextIn=d.next_in_secs!=null ? Math.max(0, d.next_in_secs - elapsed) : null;
    $(timerId).innerHTML=fmtCountdown(nextIn);
    $(subId).textContent='last run '+fmtLastRun(d.last_run);
  }
}

async function fetchAgentTimers(){
  try{
    _timerData=await fetch('/api/agent_timers').then(r=>r.json());
    _timerFetchedAt=Date.now()/1000;
    tickTimers();
  }catch(e){console.error('agent timers fetch failed',e);}
}

fetchAgentTimers();
setInterval(tickTimers,1000);
setInterval(fetchAgentTimers,60000);  // re-sync from server every minute

// ------------------------------------------------------------------ //
//  Code Review — run now button                                       //
// ------------------------------------------------------------------ //
let _reviewPollInterval=null;

async function runCodeReviewNow(){
  const btn=$('cr-runnow-btn');
  const status=$('cr-runnow-status');
  btn.disabled=true;
  status.textContent='Starting…';
  status.style.color='#ffd740';
  try{
    const r=await fetch('/api/code_review/run_now',{method:'POST'});
    const d=await r.json();
    if(!d.ok){
      status.textContent='Error: '+(d.error||'unknown');
      status.style.color='#ff5252';
      btn.disabled=false;
      return;
    }
  }catch(e){
    status.textContent='Request failed: '+e;
    status.style.color='#ff5252';
    btn.disabled=false;
    return;
  }
  status.textContent='Running — this takes ~60s…';
  if(_reviewPollInterval)clearInterval(_reviewPollInterval);
  _reviewPollInterval=setInterval(async()=>{
    try{
      const d=await fetch('/api/code_review/run_now/status').then(r=>r.json());
      if(!d.running){
        clearInterval(_reviewPollInterval);_reviewPollInterval=null;
        status.textContent='Done ✓ — refreshing results…';
        status.style.color='#00e676';
        btn.disabled=false;
        await fetchCodeReview();
        setTimeout(()=>{status.textContent='';},4000);
      }
    }catch(e){console.error('review poll error',e);}
  },3000);
}

fetchAll();
setInterval(fetchAll,3000);
</script>
</body>
</html>"""
