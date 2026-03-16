# Polymarket Arb Bot

Autonomous paper-trading bot for Polymarket implementing all four major arbitrage strategies.

## Strategies

| Strategy | Description | Avg Edge |
|---|---|---|
| **Market Rebalancing** | Buys all outcomes when sum < $1.00 in a single market | 0.3–2% |
| **Combinatorial** | Exploits logical price contradictions across related markets | 0.5–3% |
| **Latency Arb** | Trades when Polymarket crypto markets lag Binance real-time price | 0.8–5% |
| **Market Making** | Posts limit orders on both sides; earns the spread | ~0.2% of volume |

## Quickstart (Local)

```bash
# 1. Clone and enter directory
cd polymarket-arb-bot

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — defaults are fine for paper trading

# 5. Run
python bot.py
```

## Deploy on Railway

```bash
# 1. Install Railway CLI
npm i -g @railway/cli

# 2. Login and init
railway login
railway init

# 3. Set environment variables (Railway dashboard or CLI)
railway variables set TRADING_MODE=paper
railway variables set PAPER_STARTING_BALANCE_USDC=10000
# ... (copy from .env.example)

# 4. Deploy
railway up
```

## Project Structure

```
polymarket-arb-bot/
├── bot.py                        # Main orchestrator / entry point
├── src/
│   ├── models.py                 # Pydantic data models
│   ├── data/
│   │   ├── polymarket_client.py  # Gamma + CLOB API client
│   │   └── price_feed.py         # Binance WebSocket price feed
│   ├── strategies/
│   │   ├── rebalancing.py        # Market Rebalancing Arb
│   │   ├── combinatorial.py      # Cross-market Logical Arb
│   │   ├── latency_arb.py        # Latency / Price Lag Arb
│   │   └── market_making.py      # Market Making
│   ├── execution/
│   │   └── paper_engine.py       # Paper trading simulator
│   └── utils/
│       └── dashboard.py          # Rich terminal dashboard
├── logs/
│   ├── bot.log                   # Rotating log file
│   └── stats.json                # Live portfolio stats checkpoint
├── .env.example                  # Config template
├── Dockerfile                    # Railway/Docker deployment
└── railway.toml                  # Railway config
```

## Configuration

All tuning knobs live in `.env`:

```
REBALANCING_MIN_PROFIT_PCT=0.003    # minimum 0.3% net profit to trade
COMBINATORIAL_SIMILARITY_THRESHOLD=0.72  # semantic match threshold
LATENCY_ARB_PRICE_LAG_THRESHOLD=0.008    # 0.8% lag triggers entry
MM_SPREAD_TARGET=0.004              # target 0.4% spread for MM
MAX_TOTAL_EXPOSURE_USDC=2000        # max open positions
PAPER_STARTING_BALANCE_USDC=10000  # starting paper balance
```

## Transitioning to Live Trading

The bot is designed for live trading as a future upgrade. The paper engine in
`src/execution/paper_engine.py` can be replaced with a live execution engine
that signs Polygon transactions via the Polymarket CLOB API. You would need:

1. A funded wallet (USDC on Polygon)
2. Polymarket API credentials (from clob.polymarket.com)
3. A signing key for on-chain order submission

Set `TRADING_MODE=live` and implement `src/execution/live_engine.py` following
the same interface as `PaperEngine`.

## Output

The dashboard refreshes every 5 seconds in the terminal:
- Portfolio balance, PnL, return %
- Per-strategy trade count and PnL
- Live trade log with entry/profit
- Opportunity feed (scanned every 30s)

`logs/stats.json` is updated after every scan cycle for external monitoring.

## Risk Notes

- Paper trading is risk-free, but real arbitrage on Polymarket has execution risk
- Opportunities disappear in 2–3 seconds on average; speed matters in live mode
- Combinatorial arb has non-atomic execution risk (one leg may not fill)
- Always risk-size: never >10% of capital in a single market (configured by default)
