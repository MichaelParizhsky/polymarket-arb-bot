# /insights

Generate AI-powered market insights using Perplexity and Grok integrations.

## Steps

1. **Check AI integrations are live**
   - Verify `PERPLEXITY_API_KEY` and `GROK_API_KEY` are set in Railway
   - If not set, remind user and stop

2. **Run multi-source research** (do all in parallel)

   **Perplexity Sonar** — current prediction market landscape:
   - "What political, crypto, and sports events are most actively traded on Polymarket right now?"
   - "What major news events in the last 48 hours could affect prediction market prices?"

   **Perplexity Agent** — deep opportunity scan:
   - "Find prediction markets on Polymarket that appear mispriced relative to current news and public consensus probability estimates"

   **Grok X sentiment** — crowd pulse:
   - BTC, ETH, SOL sentiment from X/Twitter in last 24h
   - Any viral prediction market discussions

3. **Cross-reference with bot state**
   - Read `logs/portfolio_state.json` — which markets are we currently in?
   - Identify any overlap between AI insights and existing positions
   - Flag any open positions where news contradicts our bet

4. **Output format**

```
# Market Insights — [date]

## Hot Opportunities
- [market]: [probability gap] — [reasoning]

## News Alerts
- [headline]: impacts [market category]

## Crypto Sentiment (Grok X)
- BTC: [direction] [strength] — [summary]
- ETH: [direction] [strength] — [summary]

## Position Review
- [token]: holding [YES/NO], news says [bullish/bearish]

## Recommended Actions
- [specific strategy adjustments or markets to watch]
```
