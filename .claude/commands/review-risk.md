# /review-risk

Audit the bot's current risk configuration and flag anything dangerous.

## Steps

1. **Read current config**
   - Read `config.py` — focus on `RiskConfig` and `StrategyConfig`
   - Read `src/risk/risk_manager.py` — check hard stop thresholds

2. **Read live state**
   - Read `logs/portfolio_state.json` — check current positions, drawdown, realized P&L
   - Read most recent `logs/meta_agent_*.json` — what did the meta-agent last change?

3. **Evaluate key risk parameters**

   | Parameter | Safe Range | Flag If |
   |---|---|---|
   | MIN_EDGE_THRESHOLD | 0.02 – 0.05 | < 0.015 |
   | MAX_POSITION_SIZE | 100 – 500 | > 1000 |
   | MAX_TOTAL_EXPOSURE | 1000 – 5000 | > 8000 |
   | KELLY_FRACTION | 0.10 – 0.25 | > 0.40 |
   | DRAWDOWN_HARD_STOP | 0.10 – 0.20 | > 0.25 |
   | MIN_TRADE_INTERVAL | 15 – 120s | < 10s |

4. **Check strategy-level budgets**
   - Review `strategy_loss_budget` in config — is any strategy overexposed?
   - Cross-reference with per-strategy P&L in portfolio_state.json

5. **Report**
   - List any red flags with specific values
   - Suggest corrective `railway variables set` commands for anything out of range
   - Note what the meta-agent has auto-changed vs. manual config

## Output Format

Produce a short risk report:
```
RISK REVIEW — [date]
Status: GREEN / YELLOW / RED

Parameters:
  MIN_EDGE: 0.025 ✓
  MAX_EXPOSURE: $4,200 / $5,000 ✓
  Drawdown: -2.1% (hard stop at -15%) ✓

Flags:
  [none] or [list issues]

Recommended actions:
  [none] or [specific commands]
```
