# /deploy-check

Pre-deployment checklist for the Polymarket arb bot.

Run through the following before any `railway up` or variable change:

## Steps

1. **Check current bot state**
   - Run: `railway status` and `railway logs --tail 20`
   - Confirm paper trading is ON unless explicitly going live
   - Check `logs/portfolio_state.json` for open positions (don't deploy mid-trade)

2. **Verify config sanity**
   - Run: `python -c "from config import BotConfig; c = BotConfig(); print('Config OK:', c.paper_trading, c.risk.max_total_exposure)"`
   - Confirm MIN_EDGE_THRESHOLD ≥ 0.02
   - Confirm MAX_POSITION_SIZE ≤ 500 (paper) or ≤ 200 (live)

3. **Lint and syntax check**
   - Run: `python -m ruff check src/ main.py config.py`
   - Run: `python -m py_compile main.py config.py`

4. **Check for uncommitted secrets**
   - Run: `git status` — ensure no .env files are staged
   - Run: `git diff --stat HEAD` — review what's changed

5. **Strategy flags**
   - Confirm only intended strategies are enabled
   - Check that `STRATEGY_CROSS_EXCHANGE=False` unless Kalshi creds are set

6. **Deploy**
   - `railway up --detach` (use --detach to avoid blocking)
   - Wait 30s then check: `railway logs --tail 30`

## Red Flags — Abort Deploy If:
- `PAPER_TRADING=False` and you didn't intend to go live
- `MIN_EDGE_THRESHOLD < 0.01`
- `MAX_TOTAL_EXPOSURE > 10000`
- Any import error in syntax check
- Open positions in portfolio_state.json
