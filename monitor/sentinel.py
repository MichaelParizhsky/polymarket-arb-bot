"""
SENTINEL — Polymarket Bot B Terminal Dashboard
===============================================
Usage: python monitor/sentinel.py
Env:   DASHBOARD_URL=https://your-bot.railway.app   (default: http://localhost:5000)
       DASHBOARD_API_KEY=your-key                    (optional)
       REFRESH_INTERVAL=3                            (seconds, default 3)

Press Ctrl+C to exit.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone

# ── Windows: force UTF-8 so box-drawing / block chars render correctly ────────
if sys.platform == "win32":
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except AttributeError:
        pass

import httpx
from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ─── Styles ──────────────────────────────────────────────────────────────────
G      = "bold green"
R      = "bold red"
DIM    = "dim white"
CYAN   = "bold cyan"
MAG    = "bold magenta"
YEL    = "yellow"
BORDER = "dim green"

# ─── Config ──────────────────────────────────────────────────────────────────
DASHBOARD_URL     = os.environ.get("DASHBOARD_URL", "http://localhost:5000").rstrip("/")
DASHBOARD_API_KEY = os.environ.get("DASHBOARD_API_KEY", "")
REFRESH_INTERVAL  = float(os.environ.get("REFRESH_INTERVAL", "3"))

console = Console(force_terminal=True, emoji=True)

# ─── Shared state (mutated by refresh_data, read by renderers) ───────────────
_s: dict = {
    "status":          {},
    "system":          {},
    "positions":       [],
    "pnl_history":     [],
    "logs":            [],
    "analytics":       {},
    "strategy_pnl":    {},
    "strategy_trades": {},
    "meta_latest":     {},
    "agent_timers":    {},
    "error":           None,
    "last_refresh":    None,
    "flash_red":       False,
}


# ─── HTTP helpers ─────────────────────────────────────────────────────────────
def _headers() -> dict:
    h: dict = {"Accept": "application/json"}
    if DASHBOARD_API_KEY:
        h["X-Api-Key"] = DASHBOARD_API_KEY
    return h


async def _get(client: httpx.AsyncClient, path: str) -> dict | list:
    try:
        r = await client.get(f"{DASHBOARD_URL}{path}", headers=_headers(), timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"__error__": str(exc)}


def _err(v: object) -> bool:
    return isinstance(v, Exception) or (isinstance(v, dict) and "__error__" in v)  # type: ignore[arg-type]


# ─── Data refresh ─────────────────────────────────────────────────────────────
async def refresh_data() -> None:
    async with httpx.AsyncClient() as client:
        (status, system, positions, pnl_history, logs,
         analytics, strat_pnl, strat_trades, meta, timers) = await asyncio.gather(
            _get(client, "/api/status"),
            _get(client, "/api/system"),
            _get(client, "/api/positions"),
            _get(client, "/api/pnl_history"),
            _get(client, "/api/logs"),
            _get(client, "/api/analytics"),
            _get(client, "/api/strategy_pnl"),
            _get(client, "/api/strategy_trades"),
            _get(client, "/api/meta/latest"),
            _get(client, "/api/agent_timers"),
        )

    all_results = [status, system, positions, pnl_history, logs,
                   analytics, strat_pnl, strat_trades, meta, timers]
    errors = [r for r in all_results if _err(r)]
    _s["error"] = (
        next(str(r) if isinstance(r, Exception) else r.get("__error__", "?")  # type: ignore[union-attr]
             for r in errors)
        if errors else None
    )

    if not _err(status):       _s["status"]          = status or {}          # type: ignore[assignment]
    if not _err(system):       _s["system"]          = system or {}          # type: ignore[assignment]
    if not _err(positions):    _s["positions"]       = positions if isinstance(positions, list) else []
    if not _err(pnl_history):  _s["pnl_history"]     = pnl_history if isinstance(pnl_history, list) else []
    if not _err(logs):         _s["logs"]            = (logs if isinstance(logs, list) else [])[-40:]
    if not _err(analytics):    _s["analytics"]       = analytics or {}       # type: ignore[assignment]
    if not _err(strat_pnl):    _s["strategy_pnl"]   = strat_pnl if isinstance(strat_pnl, dict) else {}
    if not _err(strat_trades): _s["strategy_trades"] = strat_trades if isinstance(strat_trades, dict) else {}
    if not _err(meta):         _s["meta_latest"]     = meta if isinstance(meta, dict) else {}  # type: ignore[assignment]
    if not _err(timers):       _s["agent_timers"]    = timers if isinstance(timers, dict) else {}  # type: ignore[assignment]

    _s["last_refresh"] = datetime.now()
    risk = _s["system"].get("risk", {})
    _s["flash_red"] = risk.get("hard_stop", False) or risk.get("health_grade", "") in ("CRITICAL", "F")


# ─── Utility formatters ───────────────────────────────────────────────────────
def _fmt_secs(secs: float | None) -> str:
    if secs is None:
        return "never"
    s = int(secs)
    if s <= 0:
        return "now"
    h, rem = divmod(s, 3600)
    m, sc  = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {sc:02d}s" if m else f"{sc}s")


def _ago(secs: float | None) -> str:
    if secs is None:
        return "never"
    return _fmt_secs(secs) + " ago"


def _days_until(iso: str) -> str:
    if not iso:
        return "?"
    try:
        end = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        d   = (end - now).days
        return f"{d}d" if d >= 0 else "EXP"
    except Exception:
        return "?"


# ─── P&L Sparkline (filled area chart) ───────────────────────────────────────
def _sparkline(history: list, width: int = 52, height: int = 6) -> Text:
    values = [float(h.get("pnl", 0)) for h in history if "pnl" in h]
    if len(values) < 2:
        return Text("  No P&L history yet\n", style=DIM)

    if len(values) > width:
        values = values[-width:]
    n = len(values)

    mn, mx = min(values), max(values)
    rng = max(abs(mx - mn), 0.01)

    is_up = values[-1] >= values[0]
    fill  = "green" if is_up else "red"
    line  = "bold green" if is_up else "bold red"

    def v2r(v: float) -> int:
        """Map value to row index: 0=top=highest, height-1=bottom=lowest."""
        norm = (v - mn) / rng
        return height - 1 - round(norm * (height - 1))

    col_rows = [v2r(v) for v in values]

    text = Text()
    LABEL = 12

    for r in range(height):
        if r == 0:
            lbl = f" ${mx:>+8,.1f} │"
        elif r == height - 1:
            lbl = f" ${mn:>+8,.1f} │"
        elif r == height // 2:
            lbl = f" ${(mn+mx)/2:>+8,.1f} │"
        else:
            lbl = f"            │"
        text.append(lbl, style=DIM)

        for c in range(n):
            cr = col_rows[c]
            if cr == r:
                text.append("●", style=line)
            elif r > cr:
                text.append("░", style=fill)
            else:
                text.append(" ")
        text.append("\n")

    text.append(" " * LABEL + "└" + "─" * n + "\n", style=DIM)

    cur   = values[-1]
    delta = cur - values[0]
    s1    = "+" if cur   >= 0 else ""
    s2    = "+" if delta >= 0 else ""
    trend = "▲" if is_up else "▼"
    text.append(
        f"  {trend} PNL {s1}${cur:,.2f}  ·  Δ {s2}${delta:,.2f}  ·  {n} pts",
        style=f"bold {fill}",
    )
    return text


# ─── PANEL: TOP BAR ──────────────────────────────────────────────────────────
def build_top() -> Panel:
    st  = _s["status"]
    sys = _s["system"]
    rsk = sys.get("risk", {})

    bal    = st.get("balance",          0.0)
    tval   = st.get("total_value",      bal)
    pnl    = st.get("pnl",              0.0)
    ppct   = st.get("pnl_pct",          0.0)
    rpnl   = st.get("realized_pnl",     0.0)
    rpct   = st.get("realized_pnl_pct", 0.0)
    upnl   = pnl - rpnl  # unrealized
    trades = st.get("total_trades",     0)
    opos   = st.get("open_positions",   0)
    cpos   = st.get("closed_positions", 0)
    exp    = st.get("exposure",         0.0)
    fees   = st.get("fees_paid",        0.0)
    tph    = st.get("trades_per_hour",  0.0)
    cyc    = st.get("cycle_count",      0)
    upt    = st.get("uptime",           "--:--:--")
    wr     = st.get("win_rate",         0.0)
    paper  = st.get("paper_trading",    True)
    grade  = rsk.get("health_grade",    "?")
    dd     = rsk.get("drawdown_pct",    0.0)
    exp_p  = rsk.get("exposure_pct",    0.0)

    mode_s = "[yellow]◈ PAPER[/yellow]" if paper else "[bold green]◈ LIVE[/bold green]"
    gc     = "green" if grade in ("A","B") else ("yellow" if grade in ("C","D") else "red")
    pc     = "green" if pnl  >= 0 else "red"
    rc     = "green" if rpnl >= 0 else "red"
    uc     = "green" if upnl >= 0 else "red"
    dc     = "green" if dd   <  5 else ("yellow" if dd < 10 else "red")
    wc     = "green" if wr   >= 60 else "yellow"
    ec     = "green" if exp  < 3000 else "yellow"
    sync   = "[dim red]⚠ DISCONNECTED[/dim red]" if _s["error"] else "[green]◈ NET SYNC[/green]"

    def sep() -> None:
        r1.append("  │  ", style=BORDER)

    # Row 1: identity + financials
    r1 = Text()
    r1.append("◈ SENTINEL ", style="bold white")
    r1.append("│ ", style=BORDER)
    r1.append("BOT B ", style=CYAN)
    r1.append("│ ", style=BORDER)
    r1.append(f"{mode_s} ")
    sep()
    r1.append(f"HEALTH [{grade}]", style=f"bold {gc}")
    sep()
    r1.append(f"BAL ${bal:,.2f}", style=CYAN)
    sep()
    r1.append(f"VALUE ${tval:,.2f}", style="white")
    sep()
    ps = "+" if pnl  >= 0 else ""
    r1.append(f"PNL {ps}${pnl:,.2f} ({ps}{ppct:.1f}%)", style=f"bold {pc}")
    sep()
    rs = "+" if rpnl >= 0 else ""
    r1.append(f"REALIZED {rs}${rpnl:,.2f} ({rs}{rpct:.1f}%)", style=f"bold {rc}")
    sep()
    us = "+" if upnl >= 0 else ""
    r1.append(f"UNREALIZED {us}${upnl:,.2f}", style=f"{uc}")
    sep()
    r1.append(f"DD {dd:.1f}%", style=f"bold {dc}")
    sep()
    r1.append(sync)
    if _s["flash_red"]:
        r1.append("  ⛔ HARD STOP", style="bold red on white")

    # Row 2: ops metrics
    r2 = Text()
    r2.append(f"TRADES {trades}", style="white")
    r2.append("  │  ", style=BORDER)
    r2.append(f"OPEN {opos}", style=CYAN)
    r2.append("  │  ", style=BORDER)
    r2.append(f"CLOSED {cpos}", style=DIM)
    r2.append("  │  ", style=BORDER)
    r2.append(f"WIN {wr:.1f}%", style=f"bold {wc}")
    r2.append("  │  ", style=BORDER)
    r2.append(f"EXP ${exp:,.0f} ({exp_p:.1f}%)", style=ec)
    r2.append("  │  ", style=BORDER)
    r2.append(f"FEES ${fees:,.2f}", style=DIM)
    r2.append("  │  ", style=BORDER)
    r2.append(f"TPH {tph:.1f}", style=DIM)
    r2.append("  │  ", style=BORDER)
    r2.append(f"CYCLES {cyc}", style=DIM)
    r2.append("  │  ", style=BORDER)
    r2.append(f"UP {upt}", style=DIM)
    if _s["last_refresh"]:
        r2.append(f"  │  {_s['last_refresh'].strftime('%H:%M:%S')}", style=DIM)

    bg = "on #200000" if _s["flash_red"] else "on #070707"
    return Panel(Group(r1, r2), style=bg, border_style="dim green", padding=(0, 1))


# ─── PANEL: EVENTS (left) ────────────────────────────────────────────────────
def build_events() -> Panel:
    txt = Text()

    if _s["error"]:
        txt.append("⚠ DISCONNECTED\n", style="bold red")
        txt.append(f"  {str(_s['error'])[:56]}\n", style="dim red")
        txt.append("  Retrying...\n\n", style="dim yellow")

    logs = _s["logs"]
    if not logs and not _s["error"]:
        txt.append("\n  Waiting for log data...", style=DIM)

    for entry in logs[-36:]:
        if isinstance(entry, dict):
            lvl = entry.get("level", "INFO").upper()
            msg = entry.get("message", str(entry))
        else:
            msg = str(entry)
            lvl = ("ERROR"   if "ERROR" in msg.upper() else
                   "WARNING" if "WARN"  in msg.upper() else
                   "DEBUG"   if "DEBUG" in msg.upper() else "INFO")

        if lvl == "ERROR":
            sty, pfx = "bold red",  "✗ "
        elif lvl in ("WARNING", "WARN"):
            sty, pfx = YEL,         "⚠ "
        elif lvl == "DEBUG":
            sty, pfx = "dim",       "· "
        else:
            sty, pfx = DIM,         "  "

        txt.append((pfx + msg)[:54] + "\n", style=sty)

    return Panel(txt, title="[bold green]◈ EVENTS[/bold green]", border_style=BORDER, padding=(0, 1))


# ─── PANEL: CENTER (metrics + strategies + chart + agent status) ─────────────
def build_center() -> Panel:
    st   = _s["status"]
    sys  = _s["system"]
    ana  = _s["analytics"]
    rsk  = sys.get("risk", {})
    strats_cfg  = sys.get("strategies", {})
    strat_pnl   = _s["strategy_pnl"]
    strat_trades = _s["strategy_trades"]
    strat_roi   = ana.get("strategy_roi",        {})
    strat_wr    = ana.get("strategy_win_rates",  {})
    strat_fees  = ana.get("strategy_fees",       {})
    edge_dist   = ana.get("edge_distribution",   {})
    meta        = _s["meta_latest"]
    timers      = _s["agent_timers"]
    disk        = sys.get("disk",                {})
    meta_info   = sys.get("meta_agent",          {})

    pnl     = st.get("pnl",       0.0)
    bal     = st.get("balance",   0.0)
    wr      = st.get("win_rate",  0.0)
    dd      = rsk.get("drawdown_pct",  0.0)
    exp_p   = rsk.get("exposure_pct",  0.0)
    n_pos   = len(_s["positions"])
    fees    = st.get("fees_paid", 0.0)

    pc  = "green" if pnl >= 0 else "red"
    wc  = "green" if wr  >= 60 else "yellow"
    dc  = "green" if dd  <   5 else ("yellow" if dd < 10 else "red")

    # ── 1. Big stats grid ─────────────────────────────────────────────────
    grid = Table.grid(padding=(0, 3))
    for _ in range(6):
        grid.add_column(justify="center", min_width=11)

    ps = "+" if pnl >= 0 else ""
    grid.add_row(
        Text(f"${bal:,.0f}",        style=CYAN),
        Text(f"{ps}${pnl:,.2f}",    style=f"bold {pc}"),
        Text(f"{wr:.1f}%",          style=f"bold {wc}"),
        Text(f"{dd:.1f}%",          style=f"bold {dc}"),
        Text(f"${fees:,.2f}",       style=DIM),
        Text(f"{n_pos}",            style=CYAN),
    )
    grid.add_row(*[Text(h, style=DIM) for h in
                   ("BALANCE", "P&L", "WIN RATE", "DRAWDOWN", "FEES PAID", "OPEN POS")])

    # ── 2. Strategies table ───────────────────────────────────────────────
    all_names = list(strats_cfg.keys()) or list(strat_pnl.keys())
    max_pnl   = max((abs(float(strat_pnl.get(n, 0))) for n in all_names), default=1.0) or 1.0

    tbl = Table(
        box=box.SIMPLE, border_style=BORDER, header_style="bold dim green",
        show_header=True, padding=(0, 1), expand=True,
    )
    tbl.add_column("STRATEGY",  style="white",   min_width=17)
    tbl.add_column("TRD",       justify="right",  style=DIM,  min_width=4)
    tbl.add_column("WIN%",      justify="right",  style=DIM,  min_width=5)
    tbl.add_column("ROI%",      justify="right",  style=DIM,  min_width=5)
    tbl.add_column("P&L",       justify="right",             min_width=9)
    tbl.add_column("CONTRIB",                                min_width=12)
    tbl.add_column("●", justify="center",                    min_width=4)

    for name in all_names[:12]:
        cfg     = strats_cfg.get(name, {})
        enabled = cfg.get("enabled", True)
        s_pnl   = float(strat_pnl.get(name,    0.0))
        s_trd   = int(strat_trades.get(name,   0))
        s_roi   = strat_roi.get(name,          0.0)
        s_wr    = strat_wr.get(name,           0.0)
        sc      = "green" if s_pnl >= 0 else "red"
        ss      = "+" if s_pnl >= 0 else ""

        ratio  = abs(s_pnl) / max_pnl
        filled = int(ratio * 10)
        bar    = Text()
        bar.append("█" * filled,       style=f"bold {sc}")
        bar.append("░" * (10-filled),  style="dim")

        dot    = Text("ON",  style="bold green") if enabled else Text("off", style="dim red")
        wr_c   = "green" if s_wr >= 60 else ("yellow" if s_wr > 0 else DIM)
        roi_c  = "green" if s_roi > 0  else ("red" if s_roi < 0 else DIM)

        tbl.add_row(
            name.replace("_", " ").upper()[:16],
            str(s_trd) if s_trd else "—",
            Text(f"{s_wr:.0f}" if s_wr else "—", style=wr_c),
            Text(f"{s_roi:+.1f}" if s_roi else "—", style=roi_c),
            Text(f"{ss}${s_pnl:,.2f}", style=f"bold {sc}"),
            bar,
            dot,
        )

    if not all_names:
        tbl.add_row("[dim]No data[/dim]", "—", "—", "—", Text("$0.00"), Text("░"*10, style="dim"), Text("?"))

    # ── 3. Edge distribution mini-bar ────────────────────────────────────
    edge_line = Text()
    if any(v > 0 for v in edge_dist.values()):
        edge_line.append("EDGE DIST  ", style=DIM)
        total_edge = sum(edge_dist.values()) or 1
        buckets    = ["0-1%", "1-2%", "2-3%", "3-5%", "5-10%", "10%+"]
        for b in buckets:
            cnt = edge_dist.get(b, 0)
            pct = cnt / total_edge
            bar_w = max(1, int(pct * 6))
            c = "green" if b in ("5-10%", "10%+") else ("yellow" if b in ("2-3%", "3-5%") else "dim white")
            edge_line.append(f"{b}:", style=DIM)
            edge_line.append("█" * bar_w, style=c)
            edge_line.append(f"{cnt} ", style=DIM)

    # ── 4. P&L chart ──────────────────────────────────────────────────────
    chart = _sparkline(_s["pnl_history"], width=52, height=6)

    # ── 5. Bottom stats strip ─────────────────────────────────────────────
    btm = Text()
    btm.append(f"WIN {wr:.1f}%",  style=f"bold {wc}")
    btm.append("  │  ", style=BORDER)
    btm.append(f"DD {dd:.1f}%",   style=f"bold {dc}")
    btm.append("  │  ", style=BORDER)
    btm.append(f"EXP% {exp_p:.1f}%", style="white")
    btm.append("  │  ", style=BORDER)
    btm.append(f"OPEN {n_pos}",   style=CYAN)
    btm.append("  │  ", style=BORDER)
    btm.append(f"FEES ${fees:,.2f}", style=DIM)

    # ── 6. Meta-agent + agent timers ─────────────────────────────────────
    meta_enabled     = meta_info.get("enabled", False)
    meta_ago_min     = meta_info.get("last_run_ago_minutes")
    meta_interval    = meta_info.get("interval_minutes", 30)
    ta  = timers.get("meta_agent", {})
    ra  = timers.get("research",   {})
    ca  = timers.get("code_review",{})

    meta_line = Text()
    meta_line.append("META-AGENT ", style="bold white")
    meta_line.append("● " if meta_enabled else "○ ",
                     style="bold green" if meta_enabled else "dim red")
    if meta_ago_min is not None:
        meta_line.append(f"last {meta_ago_min:.0f}min ago", style=DIM)
    else:
        meta_line.append("never run", style="dim yellow")
    meta_line.append(f"  every {meta_interval}min  ", style=DIM)
    nxt = ta.get("next_in_secs")
    if nxt is not None:
        meta_line.append(f"│  next {_fmt_secs(nxt)}", style="dim cyan")

    timer_line = Text()
    timer_line.append("RESEARCH ", style=DIM)
    rn = ra.get("next_in_secs")
    timer_line.append(f"next {_fmt_secs(rn)}  ", style=DIM)
    timer_line.append("│  CODE-REVIEW ", style=BORDER)
    cn = ca.get("next_in_secs")
    timer_line.append(f"next {_fmt_secs(cn)}  ", style=DIM)
    log_mb  = disk.get("log_files_mb",    0)
    log_cnt = disk.get("log_files_count", 0)
    timer_line.append(f"│  DISK {log_mb:.1f}MB / {log_cnt} files", style=DIM)

    # ── 7. Last meta-agent insight snippet ──────────────────────────────
    insight = Text()
    if meta.get("found"):
        raw = (meta.get("analysis") or meta.get("summary") or
               meta.get("recommendation") or meta.get("report") or "")
        if isinstance(raw, dict):
            raw = raw.get("summary", str(raw))
        snip = str(raw).replace("\n", " ").strip()[:110]
        if snip:
            insight.append("▸ ", style="dim green")
            insight.append(snip, style="italic dim white")

    return Panel(
        Group(
            grid,
            Text(""),
            tbl,
            Text(""),
            edge_line if any(v > 0 for v in edge_dist.values()) else Text(""),
            Text("  P&L CHART", style="bold dim green"),
            chart,
            Text(""),
            btm,
            Text(""),
            meta_line,
            timer_line,
            insight,
        ),
        title="[bold green]◈ METRICS  ·  STRATEGIES  ·  P&L[/bold green]",
        border_style=BORDER,
        padding=(0, 1),
    )


# ─── PANEL: RIGHT (positions + connections + AI intel) ───────────────────────
def build_right() -> Panel:
    positions = _s["positions"]
    sys       = _s["system"]
    conns     = sys.get("connections",  {})
    api_keys  = sys.get("api_keys",     {})
    ai_res    = sys.get("ai_research",  {})
    flags     = sys.get("risk", {}).get("flags", [])

    txt = Text()

    # ── Open Positions ─────────────────────────────────────────────────────
    txt.append("POSITIONS\n", style="bold dim green")
    if positions:
        max_c = max((float(p.get("contracts", 0)) for p in positions), default=1.0) or 1.0
        for pos in positions[:13]:
            q       = str(pos.get("question", "?"))[:20]
            outcome = str(pos.get("outcome",  "YES")).upper()
            contr   = float(pos.get("contracts", 0))
            basis   = float(pos.get("cost_basis", 0))
            strat   = str(pos.get("strategy",  ""))[:8]
            end_iso = str(pos.get("end_date_iso", ""))
            is_long = outcome in ("YES", "Y")
            bc      = "green" if is_long else "red"
            bar_len = max(1, int((contr / max_c) * 8))
            days    = _days_until(end_iso)

            line = Text()
            line.append(f"{q:<20} ", style=DIM)
            line.append("█" * bar_len, style=f"bold {bc}")
            line.append("░" * (8 - bar_len), style="dim")
            line.append(f" {outcome:<3}", style=f"bold {bc}")
            line.append(f" ${basis:>6,.2f}", style="white")
            line.append(f" {days}\n", style=DIM)
            txt.append_text(line)

        if len(positions) > 13:
            txt.append(f"  …+{len(positions)-13} more\n", style=DIM)
    else:
        txt.append("  No open positions\n", style=DIM)

    # ── Connections ────────────────────────────────────────────────────────
    txt.append("\nCONNECTIONS\n", style="bold dim green")
    for key, label in [("polymarket", "Polymarket"), ("binance", "Binance"), ("kalshi", "Kalshi")]:
        c      = conns.get(key, {})
        status = c.get("status", "?")
        detail = str(c.get("detail", ""))[:24]
        if status == "ok":
            dot_s, dot = "bold green", "●"
        elif status == "warn":
            dot_s, dot = YEL,          "◌"
        else:
            dot_s, dot = "bold red",   "✗"
        txt.append(f"  {dot} ", style=dot_s)
        txt.append(f"{label:<11}", style="white")
        txt.append(f"{detail}\n", style=DIM)

    # ── API Keys ───────────────────────────────────────────────────────────
    txt.append("\nAPI KEYS\n", style="bold dim green")
    for key, label in [("anthropic",  "Anthropic"),
                       ("polymarket", "Polymarket"),
                       ("kalshi_rsa", "Kalshi RSA"),
                       ("perplexity", "Perplexity"),
                       ("grok",       "Grok")]:
        ok = bool(api_keys.get(key, False))
        txt.append(f"  {'●' if ok else '○'} ", style="bold green" if ok else "dim red")
        txt.append(f"{label:<12}", style="white" if ok else DIM)
        txt.append("✓\n" if ok else "✗\n",   style="bold green" if ok else "dim red")

    # ── AI Research Intel ─────────────────────────────────────────────────
    txt.append("\nAI INTEL\n", style="bold dim green")
    for key in ("perplexity", "grok", "mirofish"):
        ai    = ai_res.get(key, {})
        label = str(ai.get("label", key.capitalize()))[:14]
        cfg   = bool(ai.get("configured", False))
        note  = str(ai.get("note", ""))[:26]
        txt.append(f"  {'●' if cfg else '○'} ", style="bold green" if cfg else "dim red")
        txt.append(f"{label:<14}", style="bold white" if cfg else DIM)
        txt.append(f"{'▸ ' + note if cfg else 'offline'}\n", style="dim green" if cfg else "dim red")

    # ── Risk Flags ────────────────────────────────────────────────────────
    if flags:
        txt.append("\nRISK FLAGS\n", style="bold red")
        for f in flags[:6]:
            txt.append(f"  ⚑ {str(f)[:38]}\n", style="bold yellow")

    return Panel(txt, title="[bold green]◈ INTEL[/bold green]", border_style=BORDER, padding=(0, 1))


# ─── Layout ───────────────────────────────────────────────────────────────────
def build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top",  size=4),
        Layout(name="body"),
    )
    layout["body"].split_row(
        Layout(name="left",   ratio=2),
        Layout(name="center", ratio=5),
        Layout(name="right",  ratio=2),
    )
    return layout


def render(layout: Layout) -> None:
    layout["top"].update(build_top())
    layout["left"].update(build_events())
    layout["center"].update(build_center())
    layout["right"].update(build_right())


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    layout = build_layout()
    try:
        await refresh_data()
    except Exception as exc:
        _s["error"] = str(exc)
    render(layout)

    with Live(layout, console=console, refresh_per_second=4, screen=True) as live:
        last = time.monotonic()
        while True:
            now = time.monotonic()
            if now - last >= REFRESH_INTERVAL:
                try:
                    await refresh_data()
                except Exception as exc:
                    _s["error"] = str(exc)
                last = now
            render(layout)
            live.update(layout)
            await asyncio.sleep(0.25)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[dim green]◈ SENTINEL shutdown[/dim green]")
