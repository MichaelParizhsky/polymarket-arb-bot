"""
Surgical reset of crypto_5m strategy trades from portfolio_state.json.
Removes trade history + open positions for crypto_5m only.
Resets strategy PnL to 0.0 and adds back realized losses to USDC balance.
All other strategy data is untouched.

Run via: railway run python scripts/reset_crypto_trades.py
"""
import json
import shutil
import sys
from pathlib import Path

STATE_PATH = Path("/app/logs/portfolio_state.json")
BACKUP_PATH = Path("/app/logs/portfolio_state.backup.json")
STRATEGY = "crypto_5m"


def main():
    if not STATE_PATH.exists():
        print(f"ERROR: {STATE_PATH} not found", file=sys.stderr)
        sys.exit(1)

    with STATE_PATH.open() as f:
        state = json.load(f)

    # Backup first
    shutil.copy2(STATE_PATH, BACKUP_PATH)
    print(f"Backup saved to {BACKUP_PATH}")

    # --- Trades ---
    trades_before = state.get("trades", [])
    crypto_trades = [t for t in trades_before if t.get("strategy") == STRATEGY]
    other_trades = [t for t in trades_before if t.get("strategy") != STRATEGY]

    crypto_realized_pnl = sum(t.get("pnl", 0.0) for t in crypto_trades)
    print(f"Removing {len(crypto_trades)} crypto_5m trades (realized PnL: {crypto_realized_pnl:+.4f} USDC)")

    state["trades"] = other_trades

    # --- Open positions ---
    positions_before = state.get("positions", {})
    crypto_positions = {k: v for k, v in positions_before.items() if v.get("strategy") == STRATEGY}
    other_positions = {k: v for k, v in positions_before.items() if v.get("strategy") != STRATEGY}

    crypto_open_cost = sum(p.get("cost_basis", 0.0) for p in crypto_positions.values())
    print(f"Removing {len(crypto_positions)} open crypto_5m positions (cost basis: {crypto_open_cost:.4f} USDC)")

    state["positions"] = other_positions

    # --- Strategy PnL ---
    strategy_pnl = state.get("strategy_pnl", {})
    old_pnl = strategy_pnl.get(STRATEGY, 0.0)
    strategy_pnl[STRATEGY] = 0.0
    state["strategy_pnl"] = strategy_pnl
    print(f"Reset strategy_pnl['{STRATEGY}']: {old_pnl:+.4f} → 0.0")

    # --- USDC balance: add back the losses (realized_pnl was negative, so subtract it) ---
    old_balance = state.get("usdc_balance", 0.0)
    # Add back realized losses and open position cost
    recovery = -crypto_realized_pnl + crypto_open_cost
    new_balance = old_balance + recovery
    state["usdc_balance"] = new_balance
    print(f"USDC balance: {old_balance:.4f} → {new_balance:.4f} (recovered {recovery:+.4f})")

    # --- Recalculate total_pnl ---
    total_pnl = sum(strategy_pnl.values())
    state["total_pnl"] = total_pnl
    print(f"total_pnl recalculated: {total_pnl:+.4f}")

    # --- Strategy trade counts ---
    strategy_trades = state.get("strategy_trades", {})
    if STRATEGY in strategy_trades:
        old_count = strategy_trades[STRATEGY]
        strategy_trades[STRATEGY] = 0
        state["strategy_trades"] = strategy_trades
        print(f"strategy_trades['{STRATEGY}']: {old_count} → 0")

    # --- Strategy wins ---
    strategy_wins = state.get("strategy_wins", {})
    if STRATEGY in strategy_wins:
        old_wins = strategy_wins[STRATEGY]
        strategy_wins[STRATEGY] = 0
        state["strategy_wins"] = strategy_wins
        print(f"strategy_wins['{STRATEGY}']: {old_wins} → 0")

    # Save
    with STATE_PATH.open("w") as f:
        json.dump(state, f, indent=2)

    print(f"\nDone. Saved to {STATE_PATH}")
    print(f"Removed: {len(crypto_trades)} trades, {len(crypto_positions)} positions")
    print(f"Kept: {len(other_trades)} trades, {len(other_positions)} positions")


if __name__ == "__main__":
    main()
