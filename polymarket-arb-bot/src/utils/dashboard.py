"""
utils/dashboard.py — Rich terminal dashboard

Renders a live-updating terminal UI showing:
  - Portfolio summary
  - Per-strategy PnL
  - Recent trades
  - Live opportunity feed
"""
from __future__ import annotations
from datetime import datetime
from typing import List

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box

from src.models import Trade, StrategyType
from src.execution.paper_engine import PaperEngine


console = Console()


def render_dashboard(engine: PaperEngine, recent_opps: List[dict]) -> None:
    console.clear()
    p   = engine.portfolio
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── Header ────────────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold cyan]POLYMARKET ARB BOT[/bold cyan]  •  [dim]Paper Trading Mode[/dim]  •  {now}",
        style="bold"
    ))

    # ── Portfolio summary ─────────────────────────────────────────────────────
    pnl_color = "green" if p.total_pnl >= 0 else "red"
    summary_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    summary_table.add_column("", style="dim")
    summary_table.add_column("", style="bold")

    summary_table.add_row("Balance",       f"${p.cash_usdc:>10,.2f}")
    summary_table.add_row("Starting",      f"${p.starting_cash:>10,.2f}")
    summary_table.add_row(
        "Total PnL",
        f"[{pnl_color}]{'+'if p.total_pnl>=0 else ''}{p.total_pnl:>9,.2f}[/{pnl_color}]"
    )
    summary_table.add_row(
        "Return",
        f"[{pnl_color}]{p.return_pct:>+9.2f}%[/{pnl_color}]"
    )
    summary_table.add_row("Total Trades",  f"{p.total_trades:>10,}")
    summary_table.add_row("Win Rate",      f"{p.win_rate*100:>9.1f}%")

    # ── Strategy breakdown ────────────────────────────────────────────────────
    strat_table = Table(
        title="Strategy PnL", box=box.SIMPLE_HEAD,
        title_style="bold yellow"
    )
    strat_table.add_column("Strategy",  style="cyan")
    strat_table.add_column("Trades",    justify="right")
    strat_table.add_column("PnL",       justify="right")

    for strat, stats in engine.strategy_stats.items():
        pnl   = stats["pnl"]
        color = "green" if pnl >= 0 else "red"
        strat_table.add_row(
            strat,
            str(stats["trades"]),
            f"[{color}]{'+'if pnl>=0 else ''}{pnl:,.2f}[/{color}]",
        )

    console.print(Columns([
        Panel(summary_table, title="Portfolio", border_style="cyan"),
        Panel(strat_table,   title="Strategies", border_style="yellow"),
    ]))

    # ── Recent trades ─────────────────────────────────────────────────────────
    trades_table = Table(box=box.SIMPLE_HEAD, title="Recent Trades", title_style="bold magenta")
    trades_table.add_column("Time",     style="dim",    width=10)
    trades_table.add_column("Strategy", style="cyan",   width=14)
    trades_table.add_column("Market",   width=38)
    trades_table.add_column("Size",     justify="right", width=10)
    trades_table.add_column("PnL",      justify="right", width=10)
    trades_table.add_column("Notes",    style="dim",    width=35)

    recent_trades = list(reversed(p.closed_trades))[:15]
    for t in recent_trades:
        pnl   = t.profit_usdc or 0
        color = "green" if pnl >= 0 else "red"
        time  = t.closed_at.strftime("%H:%M:%S") if t.closed_at else "—"
        trades_table.add_row(
            time,
            t.strategy.value,
            t.market_id[:38],
            f"${t.size_usdc:.2f}",
            f"[{color}]{'+'if pnl>=0 else ''}{pnl:.2f}[/{color}]",
            (t.notes or "")[:35],
        )

    console.print(Panel(trades_table, border_style="magenta"))

    # ── Live opportunity feed ─────────────────────────────────────────────────
    if recent_opps:
        opp_table = Table(box=box.SIMPLE, title="Recent Opportunities", title_style="bold green")
        opp_table.add_column("Time",    style="dim",  width=10)
        opp_table.add_column("Type",    style="cyan", width=14)
        opp_table.add_column("Market",  width=45)
        opp_table.add_column("Profit%", justify="right", width=10)
        opp_table.add_column("Traded",  width=8)

        for o in recent_opps[-10:]:
            opp_table.add_row(
                o.get("time", "—"),
                o.get("type", "—"),
                o.get("market", "—")[:45],
                f"{o.get('profit_pct', 0)*100:.3f}%",
                "✓" if o.get("traded") else "·",
            )
        console.print(Panel(opp_table, border_style="green"))
