#!/usr/bin/env python3
"""
Polymarket Arb Bot — Meta Agent
================================
Uses Claude claude-opus-4-6 to analyze bot performance and suggest
parameter improvements. Shows suggestions for human approval before
applying any changes.

Usage:
    python meta_agent.py              # analyze and suggest
    python meta_agent.py --apply      # analyze, suggest, then prompt to apply
    python meta_agent.py --watch 30   # run every 30 minutes
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from src.meta_agent.analyzer import PortfolioSnapshot

load_dotenv()
console = Console()

CURRENT_CONFIG_KEYS = [
    "MIN_EDGE_THRESHOLD",
    "MAX_POSITION_SIZE",
    "MAX_TOTAL_EXPOSURE",
    "MAX_SLIPPAGE",
    "MM_SPREAD_BPS",
    "MM_ORDER_SIZE",
    "MM_MAX_INVENTORY",
    "STRATEGY_REBALANCING",
    "STRATEGY_COMBINATORIAL",
    "STRATEGY_LATENCY_ARB",
    "STRATEGY_MARKET_MAKING",
]

SYSTEM_PROMPT = """You are an expert quantitative analyst specializing in prediction market arbitrage.
You analyze trading bot performance data and suggest concrete, conservative parameter improvements.

Your suggestions must:
1. Be based strictly on the data provided — no speculation
2. Be conservative — small adjustments, never radical changes
3. Never suggest increasing risk beyond what the data supports
4. Always explain the reasoning in plain English
5. Return a JSON block with suggested .env changes

When analyzing strategies:
- Rebalancing: look at win rate and edge capture — if losing, raise MIN_EDGE_THRESHOLD
- Combinatorial: check if it's finding real opportunities or false positives
- Latency arb: check trades_per_hour — too high means it's overtrading noise
- Market making: inventory risk shows up as large open positions with negative PnL

Format your response as:
1. A brief plain-English analysis (2-3 paragraphs)
2. A JSON block with ONLY the keys to change (not all keys), wrapped in ```json ... ```
3. A risk warning if any suggestion increases risk

Example JSON block:
```json
{
  "MIN_EDGE_THRESHOLD": "0.03",
  "MM_SPREAD_BPS": "75",
  "STRATEGY_LATENCY_ARB": "false"
}
```
"""


def load_current_env(env_path: str = ".env") -> dict[str, str]:
    """Read current .env values for the config keys."""
    env = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    if key.strip() in CURRENT_CONFIG_KEYS:
                        env[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    return env


def apply_env_changes(changes: dict[str, str], env_path: str = ".env") -> None:
    """Write approved changes back to .env file."""
    try:
        with open(env_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    updated_keys = set()
    new_lines = []
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=")[0].strip()
            if key in changes:
                new_lines.append(f"{key}={changes[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Add any keys not already in file
    for key, val in changes.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}\n")

    with open(env_path, "w") as f:
        f.writelines(new_lines)


def extract_json_from_response(text: str) -> dict | None:
    """Pull the JSON block out of Claude's response."""
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def print_changes_table(current: dict, proposed: dict) -> None:
    table = Table(title="Proposed Parameter Changes", show_header=True)
    table.add_column("Parameter", style="cyan")
    table.add_column("Current", style="yellow")
    table.add_column("Proposed", style="green")
    table.add_column("Change", style="white")

    for key, new_val in proposed.items():
        old_val = current.get(key, "not set")
        try:
            old_f = float(old_val)
            new_f = float(new_val)
            delta = ((new_f - old_f) / max(abs(old_f), 0.001)) * 100
            change = f"{delta:+.1f}%"
        except (ValueError, TypeError):
            change = "toggle"
        table.add_row(key, str(old_val), str(new_val), change)
    console.print(table)


def run_analysis(apply: bool = False) -> None:
    """Main analysis loop."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]Error: ANTHROPIC_API_KEY not set in .env[/red]")
        console.print("Get your key at console.anthropic.com and add it to .env")
        sys.exit(1)

    state_path = "logs/portfolio_state.json"
    if not Path(state_path).exists():
        console.print(
            f"[yellow]No portfolio state found at {state_path}.[/yellow]\n"
            "Run the bot for at least 5 minutes first (it saves state every 5 min)."
        )
        sys.exit(1)

    console.print(Panel("[bold cyan]Polymarket Meta-Agent[/bold cyan]\nLoading performance data..."))

    # Load data
    snapshot = PortfolioSnapshot.from_json(state_path)
    analysis_data = snapshot.to_analysis_dict()
    current_env = load_current_env()

    console.print(f"[dim]Data age: {snapshot.age_hours():.1f}h | "
                  f"Trades: {len(snapshot.trades)} | "
                  f"P&L: ${snapshot.total_pnl:+.2f}[/dim]\n")

    if len(snapshot.trades) < 5:
        console.print(
            "[yellow]Only {n} trades found. Run the bot longer for better analysis.[/yellow]".format(
                n=len(snapshot.trades)
            )
        )

    # Build user message for Claude
    user_message = f"""Please analyze this Polymarket arbitrage bot's performance and suggest parameter improvements.

## Current Configuration
```json
{json.dumps(current_env, indent=2)}
```

## Performance Data (last snapshot)
```json
{json.dumps(analysis_data, indent=2)}
```

Focus on:
1. Which strategies are profitable vs losing money?
2. Are position sizes appropriate for the edges being captured?
3. Is the bot overtrading (too many small losing trades) or under-trading?
4. Which strategies should be disabled or have their thresholds raised?
5. Any parameter that would reduce fees while maintaining edge?

Be conservative — this is a paper trading bot learning the markets. Small, safe adjustments only.
"""

    # Call Claude
    console.print("[dim]Calling Claude claude-opus-4-6 for analysis...[/dim]")
    client = anthropic.Anthropic(api_key=api_key)

    with console.status("[bold green]Claude is thinking...[/bold green]"):
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=2000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            response_text = stream.get_final_message().content[-1].text

    console.print("\n")
    console.print(Panel(Markdown(response_text), title="[bold]Claude's Analysis[/bold]", border_style="cyan"))

    # Extract proposed changes
    proposed = extract_json_from_response(response_text)
    if not proposed:
        console.print("[yellow]No parameter changes suggested.[/yellow]")
        return

    console.print("\n")
    print_changes_table(current_env, proposed)

    # Save suggestion log
    os.makedirs("logs", exist_ok=True)
    suggestion_log = {
        "timestamp": time.time(),
        "analysis": response_text,
        "proposed_changes": proposed,
        "current_values": current_env,
        "portfolio_snapshot": analysis_data,
    }
    log_path = f"logs/meta_agent_{int(time.time())}.json"
    with open(log_path, "w") as f:
        json.dump(suggestion_log, f, indent=2)
    console.print(f"[dim]Suggestion saved to {log_path}[/dim]\n")

    # Apply changes if requested
    if apply:
        if Confirm.ask("[bold yellow]Apply these changes to .env?[/bold yellow]"):
            apply_env_changes(proposed)
            console.print("[green]Changes applied. Restart the bot to take effect.[/green]")
        else:
            console.print("[dim]Changes not applied.[/dim]")
    else:
        console.print("[dim]Run with --apply to apply changes after reviewing.[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Arb Bot Meta-Agent")
    parser.add_argument("--apply", action="store_true", help="Prompt to apply suggested changes")
    parser.add_argument("--watch", type=int, metavar="MINUTES", help="Run every N minutes")
    args = parser.parse_args()

    if args.watch:
        console.print(f"[cyan]Watch mode: analyzing every {args.watch} minutes[/cyan]")
        while True:
            try:
                run_analysis(apply=args.apply)
            except Exception as e:
                console.print(f"[red]Analysis error: {e}[/red]")
            console.print(f"[dim]Next analysis in {args.watch} minutes...[/dim]")
            time.sleep(args.watch * 60)
    else:
        run_analysis(apply=args.apply)


if __name__ == "__main__":
    main()
