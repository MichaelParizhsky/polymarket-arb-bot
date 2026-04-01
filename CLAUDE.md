# Polymarket Arbitrage Bot — Claude Instructions

## Project Overview

Python async arbitrage bot trading Polymarket and Kalshi prediction markets.

**Architecture:**
- `main.py` — `ArbBot` orchestrator, meta-agent loop, config hot-reload
- `config.py` — All tunable params via env vars (paper/live, risk limits, strategy flags)
- `src/strategies/` — 8 strategies: rebalancing, combinatorial, latency_arb, market_making, resolution, event_driven, cross_exchange, futures_hedge
- `src/exchange/` — Polymarket (REST + WebSocket), Kalshi, Binance
- `src/risk/risk_manager.py` — Kelly-inspired sizing, drawdown hard stop (15%)
- `src/meta_agent/analyzer.py` — `PortfolioSnapshot`, per-strategy metrics fed to Claude
- `src/dashboard/app.py` — FastAPI dashboard on port 5000
- `src/utils/metrics.py` — Prometheus metrics on port 8000
- `logs/portfolio_state.json` — Live state (auto-saved every 5 min)
- `logs/meta_agent_*.json` — Claude analysis history

**Key config knobs (all via env vars):**
```
MIN_EDGE_THRESHOLD=0.02    # Min edge after fees to trade
MAX_POSITION_SIZE=500      # Max USDC per trade
MAX_TOTAL_EXPOSURE=5000    # Max total open USDC
MIN_TRADE_INTERVAL=60      # Seconds between any two trades
TOKEN_COOLDOWN=300         # Seconds before re-trading same token
MM_SPREAD_BPS=50           # Market making spread
MAX_DAYS_TO_RESOLUTION=30  # Filter markets by time-to-resolution
STRATEGY_*=True/False      # Enable/disable individual strategies
```

**Meta-agent:** Claude claude-opus-4-6 with adaptive thinking runs every 30 min, reads `logs/portfolio_state.json`, auto-applies parameter changes, logs to `logs/meta_agent_*.json`.

## Active Skills for This Project

### Market Research & Opportunity Finding
Use **`/prediction-markets-analysis`** to:
- Research specific Kalshi/Polymarket markets before sizing up a position
- Find mispricings between model probability and live market price
- Identify upcoming catalysts that could move prices
- Check historical resolution context for similar markets
- Cross-reference with event_driven strategy targets

### Strategy Performance Analysis
Use **`/analytics-tracking`** to:
- Set up structured tracking of per-strategy win rates and PnL
- Build a tracking plan for the 8 strategies
- Audit which metrics matter (don't track vanity metrics)

Use **`/campaign-analytics`** to:
- Analyze strategy performance over time periods
- Compare strategy effectiveness across market regimes (pre-election vs. post, high-vol vs. low)

Use **`/financial-metrics-analysis`** to:
- Analyze PnL growth, fee drag, and win-rate trends over time
- Compute YoY/MoM equivalents for bot revenue

### Portfolio & Risk Review
Use **`/financial-health-scores`** to:
- Score portfolio health based on drawdown, concentration, and fee drag
- Flag when a strategy is consuming too much exposure relative to PnL

Use **`/saas-metrics-coach`** to:
- Think about bot revenue in SaaS terms: daily/monthly "MRR", strategy-level churn (disable underperformers), CAC equivalent (fees paid to find edge)
- Model the bot's revenue trajectory and set targets

Use **`/scenario-war-room`** to:
- Stress test risk configs: what happens at 20% drawdown? Binance feed goes down? Kalshi API rate limit?
- Model worst-case scenarios before going live

### Event-Driven Strategy Research
Use **`/earnings-call-analysis`** + **`/sec-8k-analysis`** to:
- Find material events (earnings, guidance, 8-K filings) that create near-term prediction market catalysts
- Feed insights into the `EventDrivenStrategy` — which markets are about to see volume spikes?

Use **`/competitive-intel`** to:
- Analyze market microstructure: who else is making markets, what's the typical bid-ask spread, how liquid is each category
- Identify underserved market categories where edge is easier to find

### Market Making Optimization
Use **`/pricing-strategy`** to:
- Analyze `MM_SPREAD_BPS` relative to market liquidity and competition
- Model optimal spread for different market categories and volumes

Use **`/revenue-operations`** to:
- Optimize revenue per strategy, reduce fee drag, model the fee structure
- Build a revenue ops view of the bot treating each strategy as a revenue line

### Decision Tracking
Use **`/decision-logger`** to:
- Log key decisions made (strategy enable/disable, parameter changes from meta-agent)
- Create a structured audit trail linking config changes to performance outcomes

### Backtesting
Use **`/cbt-trading`** to:
- Backtest strategy parameter combinations (MIN_EDGE_THRESHOLD, MM_SPREAD_BPS, position sizing)
- Evaluate strategy performance across historical market data
- Optimize Kelly fraction multiplier for position sizing

## Development Rules

- Always run in paper mode (`PAPER_TRADING=True`) unless explicitly going live
- When changing risk params, read `logs/meta_agent_config.json` first — meta-agent may have already tuned them
- Test strategy changes by enabling only that strategy (disable others via env) to isolate signal
- The meta-agent auto-applies config changes — check `logs/meta_agent_*.json` to understand what's been auto-tuned before changing manually
- Strategies use `combo_min_edge` from `StrategyConfig` for cross-exchange min edge (same knob as combinatorial)
- Cross-exchange strategy disabled by default (`STRATEGY_CROSS_EXCHANGE=False`) — requires Kalshi credentials
- `FEE_RATE = 0.002` per side (hardcoded in cross_exchange.py) — factor this into any edge calculations
- **Confirmed rate limits (March 2026)**: POST /order 500 req/10s burst, 3,000 req/10min sustained (5 req/s average). MIN_TRADE_INTERVAL=15s is well within limits.
- **Taker fees by market type** (confirmed): standard=0.2% flat, crypto_5m=`0.25*(p*(1-p))^2` (max 1.56% at 50%), sports=0.30% flat, dcm=0.30% flat
- **Latency arb on 5/15-min crypto markets is dead** — dynamic fees at 50% probability exceed arb margin. QuickResolutionStrategy targets only high-conviction extremes (>88% or <12%).
- **AsyncClobClient** is available in py-clob-client 0.34.6 — future upgrade would eliminate all `run_in_executor` overhead in `polymarket.py`
- **Batch orders**: up to 15 orders per request via the CLOB batch endpoint

## Feedback Loop

When adding new strategies or changing logic, always:
1. Enable the strategy in isolation
2. Run paper mode for at least 50 cycles
3. Check `logs/portfolio_state.json` for per-strategy PnL
4. Let the meta-agent run at least one analysis cycle before tuning further

## System Architecture Notes (from production research)
- **Gas costs are zero** — Polymarket's relayer pays all Polygon gas. Bot only needs USDC. POL balance irrelevant.
- **Regime detection signal** — maker/taker flow inversion, not price patterns. Pre-2024 election: takers won (-2.9pp avg). Post-election: makers won (+2.5pp). Who is losing indicates regime.
- **Rolling Kelly requires N≥200** for stable variance estimation. Window of 20–50 causes whipsaw on normal variance. Regime reduction (WR<45%) only fires at N≥30 minimum.
- **`asyncio.gather()` creates zombie tasks** — exceptions don't cancel siblings; they run indefinitely holding connections/locks. Use `TaskGroup` for coordinated task groups; use `gather(return_exceptions=True)` only when per-task introspection is needed (strategy scans).
- **Context engineering > prompting** — model behavior is primarily a function of context state, not instruction text. What's in the window and in what order matters more than wording.
- **Meta-agent state safety** — server-side compaction is lossy. Never rely on compaction to preserve numerical state (positions, cost basis, fees). All critical state must be in `logs/portfolio_state.json` and explicitly injected into each new context.
- **`dataclass(slots=True)` gotchas** — breaks if combined with `frozen=True` (kills custom `__getstate__`), closure methods, or `__init_subclass__` keyword params. Our usage (no frozen, no closures) is safe.

## Context on Strategy Logic

| Strategy | Edge Source | Key Param |
|---|---|---|
| `rebalancing` | YES+NO price deviation from $1 | `rebalancing_min_edge=0.02` |
| `combinatorial` | Multi-outcome portfolio imbalance | `combo_min_edge=0.03` |
| `latency_arb` | Polymarket lagging Binance crypto prices | `latency_price_lag_threshold=0.015` |
| `market_making` | Earn spread as passive liquidity provider | `mm_spread_bps=50` |
| `resolution` | Mispriced near-expiry markets | `max_days_to_resolution` |
| `event_driven` | News/event catalysts moving markets | External research needed |
| `cross_exchange` | Poly vs Kalshi price divergence | `combo_min_edge` |
| `futures_hedge` | Binance futures hedge on crypto markets | `BINANCE_FUTURES_ENABLED` |
| `quick_resolution` | High-conviction near-expiry extremes (>88% or <12%) | `QUICK_RESOLUTION_MIN_CONVICTION` |
| `crypto_5m` | 5-min BTC/ETH/SOL binary markets, dual-arb + snipe | `STRATEGY_CRYPTO_5M` |
| `swarm_prediction` | Multi-persona crowd simulation via Perplexity Agent API | `STRATEGY_SWARM` |
| `live_game` | In-play momentum: buy leading team when Poly lags ESPN win prob | `STRATEGY_LIVE_GAME` |

## AI Integrations

| Integration | Purpose | Env Var |
|---|---|---|
| Perplexity Sonar | Live news for event_driven, news_monitor | `PERPLEXITY_API_KEY` |
| Perplexity Agent | Multi-step research, swarm personas | `PERPLEXITY_API_KEY` |
| Grok (xAI) | X/Twitter crypto sentiment gate in crypto_5m | `GROK_API_KEY` |
| MiroFish (optional) | Full swarm simulation REST API | `MIROFISH_URL` |

## Slash Commands (`.claude/commands/`)

- `/deploy-check` — Pre-deploy checklist before any `railway up`
- `/review-risk` — Audit risk params and flag anything dangerous
- `/session-handoff` — Generate handoff doc for next session
- `/insights` — AI-powered market insights via Perplexity + Grok

## Claude Hooks (`.claude/hooks/`)

- `protect-files.sh` — Blocks writes to `.env`, `secrets`, `railway.toml`, `get_creds.py`
- `audit-commands.sh` — Logs all bash commands to `.claude/logs/claude-audit.log`
- PostToolUse: Auto-runs `ruff check --fix` + `ruff format` on all `.py` files edited
- SessionStart: Injects git branch + last 3 commits + Railway service status

## Self-Improvement Protocol

Before any session involving >3 files or strategy changes:
1. Use Plan Mode (`/plan`) to design approach before writing code
2. Search web for latest API docs before implementing external integrations
3. Run `/review-risk` after any config change
4. Run `/deploy-check` before any `railway up`
5. Run `/session-handoff` at end of session
6. Use `/clear` between distinct tasks to keep context clean
