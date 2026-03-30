#!/usr/bin/env python3
"""
Diagnostic: simulate one full Bot B cycle against the real Polymarket API.
No credentials needed — all calls are read-only.
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# Force paper mode env vars matching Bot B
os.environ.setdefault("PAPER_TRADING", "true")
os.environ.setdefault("PAPER_SKIP_HARD_STOP", "true")
os.environ.setdefault("PAPER_SKIP_BUDGETS", "true")
os.environ.setdefault("DAILY_CLOSE_ONLY", "false")
os.environ.setdefault("MAX_DAYS_TO_RESOLUTION", "30")
os.environ.setdefault("LOG_LEVEL", "INFO")

sys.path.insert(0, os.path.dirname(__file__))

from config import CONFIG
from src.exchange.polymarket import PolymarketClient
from src.portfolio.paper_trading import PaperPortfolio
from src.risk.risk_manager import RiskManager
from src.strategies.combinatorial import CombinatorialStrategy
from src.strategies.market_making import MarketMakingStrategy
from src.strategies.resolution import ResolutionStrategy
from src.strategies.quick_resolution import QuickResolutionStrategy
from src.utils.logger import logger, setup_logger

setup_logger("INFO")


async def run_diagnostic():
    print("\n" + "=" * 70)
    print("BOT B DIAGNOSTIC — real Polymarket API, paper portfolio")
    print("=" * 70)

    portfolio = PaperPortfolio(starting_balance=10_000.0)
    risk = RiskManager(config=CONFIG, portfolio=portfolio)

    print(f"\nConfig:")
    print(f"  PAPER_TRADING        = {CONFIG.paper_trading}")
    print(f"  DAILY_CLOSE_ONLY     = {CONFIG.strategies.daily_close_only}")
    print(f"  MAX_DAYS_TO_RESOLUTION = {CONFIG.strategies.max_days_to_resolution}")
    print(f"  MIN_EDGE_THRESHOLD   = {CONFIG.risk.min_edge_threshold}")
    print(f"  MAX_POSITION_SIZE    = {CONFIG.risk.max_position_size}")
    print(f"  STARTING_BALANCE     = {CONFIG.starting_balance}")
    print(f"  combinatorial_enabled = {CONFIG.strategies.combinatorial_enabled}")
    print(f"  market_making_enabled = {CONFIG.strategies.market_making_enabled}")
    print(f"  resolution_enabled   = {CONFIG.strategies.resolution_enabled}")
    print(f"  quick_resolution_enabled = {CONFIG.strategies.quick_resolution_enabled}")
    print(f"  crypto_5m_enabled    = {getattr(CONFIG.strategies, 'crypto_5m_enabled', False)}")
    print(f"  quick_resolution_min_conviction = {CONFIG.strategies.quick_resolution_min_conviction}")

    strategies = []
    if CONFIG.strategies.combinatorial_enabled:
        strategies.append(CombinatorialStrategy(CONFIG, portfolio, risk))
    if CONFIG.strategies.market_making_enabled:
        strategies.append(MarketMakingStrategy(CONFIG, portfolio, risk))
    if CONFIG.strategies.resolution_enabled:
        strategies.append(ResolutionStrategy(CONFIG, portfolio, risk))
    if CONFIG.strategies.quick_resolution_enabled:
        strategies.append(QuickResolutionStrategy(CONFIG, portfolio, risk))

    print(f"\nStrategies loaded: {[s.__class__.__name__ for s in strategies]}")

    async with PolymarketClient(CONFIG.polymarket, paper_trading=True) as poly:
        print("\n--- Fetching markets from Polymarket API ---")
        t0 = time.perf_counter()
        markets = await poly.get_markets_cached()
        expiring = await poly.get_expiring_markets_cached(max_hours=48.0)
        elapsed = time.perf_counter() - t0
        print(f"  get_markets: {len(markets)} markets in {elapsed:.2f}s")
        print(f"  get_expiring: {len(expiring)} markets expiring within 48h")

        # Merge (same as main.py)
        seen_ids = {m.condition_id for m in expiring}
        markets = list(expiring) + [m for m in markets if m.condition_id not in seen_ids]
        print(f"  Merged total: {len(markets)} markets")

        # Apply date filter
        now_utc = datetime.now(timezone.utc)
        if CONFIG.strategies.daily_close_only:
            today_end = now_utc + timedelta(hours=24)
            filtered = []
            for m in markets:
                if not m.end_date_iso:
                    continue
                try:
                    end_ts = datetime.fromisoformat(m.end_date_iso.replace("Z", "+00:00"))
                    if now_utc <= end_ts <= today_end:
                        filtered.append(m)
                except ValueError:
                    continue
            markets = filtered
            print(f"  After DAILY_CLOSE_ONLY (24h): {len(markets)} markets")
        else:
            max_days = CONFIG.strategies.max_days_to_resolution
            if max_days > 0:
                cutoff = now_utc.timestamp() + max_days * 86400
                markets = [
                    m for m in markets
                    if m.end_date_iso and _parse_ts(m.end_date_iso) <= cutoff
                ]
                print(f"  After max_days={max_days}: {len(markets)} markets")

        # Volume sort + cap
        max_markets = CONFIG.strategies.max_markets
        markets_sorted = sorted(markets, key=lambda m: m.volume, reverse=True)[:max_markets]
        print(f"  After volume cap ({max_markets}): {len(markets_sorted)} markets")

        # Show top 5 markets
        print("\n  Top 5 markets by volume:")
        for m in markets_sorted[:5]:
            print(f"    [{m.volume:>10,.0f} vol] {m.question[:65]}")
            print(f"    {'':12} end={m.end_date_iso[:19] if m.end_date_iso else 'none'} active={m.active}")

        # Fetch orderbooks for all tokens
        print(f"\n--- Fetching orderbooks ---")
        token_ids = []
        seen_tok = set()
        for m in markets_sorted:
            for t in m.tokens:
                if t.token_id not in seen_tok:
                    seen_tok.add(t.token_id)
                    token_ids.append(t.token_id)

        print(f"  Total tokens: {len(token_ids)}")
        t0 = time.perf_counter()
        orderbooks = {}
        batch_size = 30
        fail_count = 0
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i:i + batch_size]
            results = await asyncio.gather(
                *[poly.get_orderbook(tid) for tid in batch],
                return_exceptions=True
            )
            for tid, res in zip(batch, results):
                if isinstance(res, Exception):
                    fail_count += 1
                else:
                    orderbooks[tid] = res
        elapsed = time.perf_counter() - t0
        print(f"  Fetched {len(orderbooks)}/{len(token_ids)} orderbooks in {elapsed:.2f}s ({fail_count} failed)")

        # Build context
        token_meta = {}
        for m in markets_sorted:
            for t in m.tokens:
                token_meta[t.token_id] = (m.question, t.outcome, getattr(m, "end_date_iso", ""), tuple(getattr(m, "tags", [])))

        context = {
            "markets": markets_sorted,
            "orderbooks": orderbooks,
            "binance_feed": None,
            "kalshi_markets": [],
            "crypto_short_markets": [],
            "token_meta": token_meta,
            "timestamp": time.time(),
        }

        # Run strategies
        print(f"\n--- Running strategies ---")
        all_signals = []
        for strategy in strategies:
            t0 = time.perf_counter()
            try:
                sigs = await strategy.scan(context) or []
            except Exception as e:
                print(f"  {strategy.__class__.__name__}: EXCEPTION: {e}")
                import traceback; traceback.print_exc()
                sigs = []
            elapsed = time.perf_counter() - t0
            print(f"  {strategy.__class__.__name__}: {len(sigs)} signals in {elapsed:.2f}s")
            for s in sigs[:3]:
                print(f"    Signal: {s.side} {s.token_id[:20]} price={s.price:.3f} size=${s.size_usdc:.2f} edge={s.edge:.3f}")
            all_signals.extend(sigs)

        print(f"\nTotal signals: {len(all_signals)}")

        # Check what risk checks do to these signals
        if all_signals:
            print("\n--- Risk check on signals ---")
            for sig in all_signals[:10]:
                ok, reason = risk.check_trade(sig.token_id, sig.side, sig.size_usdc, sig.strategy)
                status = "PASS" if ok else f"FAIL: {reason}"
                print(f"  [{sig.strategy}] {sig.side} {sig.token_id[:20]} ${sig.size_usdc:.2f} -> {status}")

        # Show risk/portfolio state
        print(f"\n--- Portfolio/Risk state ---")
        print(f"  USDC balance: ${portfolio.usdc_balance:,.2f}")
        print(f"  Hard stopped: {risk.is_hard_stopped()}")
        print(f"  Permanent lock: {risk._permanent_lock}")
        rolling_wr = risk.rolling_win_rate()
        print(f"  Rolling WR: {rolling_wr:.1%} ({len(risk._rolling_results)} trades)")
        size = risk.size_position(edge=0.05)
        print(f"  size_position(edge=0.05): ${size:.2f}")

        print("\n" + "=" * 70)
        if not all_signals:
            print("DIAGNOSIS: No signals generated. Likely causes:")
            print("  1. Markets loaded but edge conditions not met (prices not extreme enough)")
            print("  2. DAILY_CLOSE_ONLY=true filtering out most markets")
            print("  3. Specific strategy thresholds too tight for current market conditions")
        else:
            ok_count = sum(1 for s in all_signals if risk.check_trade(s.token_id, s.side, s.size_usdc, s.strategy)[0])
            print(f"DIAGNOSIS: {len(all_signals)} signals, {ok_count} pass risk check → trades WOULD fire")
        print("=" * 70)


def _parse_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


if __name__ == "__main__":
    asyncio.run(run_diagnostic())
