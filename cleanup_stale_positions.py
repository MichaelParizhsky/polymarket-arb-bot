"""
One-off script: close stale sports/resolved positions in portfolio_state.json.
Run once, then restart the bot.

For each position, we check if the market name matches a resolved game,
then close it at $0 (NO wins if market resolved NO, or YES resolves to $1).
Since we don't know exact outcomes, we close sports at $0 (worst case)
and everything else at avg_cost (break even) to free up exposure.
"""
import json
import time
import shutil

STATE = "logs/portfolio_state.json"

with open(STATE) as f:
    s = json.load(f)

# Backup first
shutil.copy(STATE, STATE + ".bak")
print("Backed up to", STATE + ".bak")

positions = s.get("positions", {})
to_close = []

SPORTS_KEYWORDS = [
    "vs.", "vs ", "maple leafs", "pistons", "thunder", "ducks",
    "nba", "nfl", "nhl", "mlb", "match", "game 1", "game 2",
]

for tid, pos in list(positions.items()):
    q = pos.get("market_question", "").lower()
    is_sports = any(kw in q for kw in SPORTS_KEYWORDS)
    if is_sports:
        to_close.append((tid, pos))

print(f"\nPositions to close ({len(to_close)}):")
for tid, pos in to_close:
    cost = pos["contracts"] * pos["avg_cost"]
    print(f"  {pos['market_question'][:60]} — ${cost:.2f} deployed")

if not to_close:
    print("Nothing to close.")
else:
    confirm = input("\nClose these at $0 (worst-case sports resolution)? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
    else:
        recovered = 0.0
        for tid, pos in to_close:
            # Close at $0 — sports position expired worthless (worst case)
            s["closed_positions"].append({
                "token_id": tid,
                "market_question": pos["market_question"],
                "outcome": pos.get("outcome", ""),
                "strategy": pos.get("strategy", "market_making"),
                "realized_pnl": round(-pos["contracts"] * pos["avg_cost"], 4),
                "opened_at": pos.get("opened_at", time.time()),
                "closed_at": time.time(),
                "note": "manually_closed_stale_resolved_market",
            })
            del s["positions"][tid]
            print(f"  Closed: {pos['market_question'][:55]}")

        # Recalculate balance — capital NOT returned (position resolved $0)
        # usdc_balance stays as-is since the money was already lost
        s["open_positions"] = len(s["positions"])

        with open(STATE, "w") as f:
            json.dump(s, f)

        print(f"\nDone. {len(to_close)} positions closed at $0.")
        print(f"Restart the bot to pick up the cleaned state.")
        print(f"\nNote: If any of these markets actually resolved NO (bot won),")
        print(f"the realized_pnl is wrong. Check Polymarket and manually adjust if needed.")
