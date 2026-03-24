# /session-handoff

Generate a concise handoff document for the next Claude session.

## Steps

1. **Read current state**
   - `logs/portfolio_state.json` — positions, P&L, drawdown
   - `git log --oneline -10` — recent commits
   - `.claude/logs/claude-audit.log` (last 20 lines) — recent commands run

2. **Summarize in this format**

```
# Session Handoff — [date]

## Bot Status
- Paper trading: [yes/no]
- Open positions: [count] worth $[total]
- Realized P&L: $[amount]
- Drawdown: [%]

## What Was Done This Session
- [bullet list of changes made]

## Files Modified
- [list with brief description of each change]

## Outstanding Issues / Next Steps
- [anything unfinished or needing follow-up]

## Railway Status
- Last deploy: [when]
- Active env vars changed: [list]

## Key Decisions Made
- [any config changes, strategy enables/disables, etc.]
```

3. **Save to `logs/session-handoff-[YYYY-MM-DD].md`**

4. Remind the user to run `/clear` before the next task to keep context clean.
