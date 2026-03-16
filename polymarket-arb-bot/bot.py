"""
bot.py — Main Bot Orchestrator

Async event loop that:
  1. Fetches Polymarket markets periodically
  2. Runs all enabled strategy scanners
  3. Executes identified opportunities via paper engine
  4. Renders live terminal dashboard
  5. Logs structured JSON output for analysis
"""
from __future__ import annotations
import asyncio
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import List

import aiohttp
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_FILE  = os.getenv("LOG_FILE", "logs/bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

logger.remove()
logger.add(sys.stderr, level=LOG_LEVEL, colorize=True,
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
logger.add(LOG_FILE, level="DEBUG", rotation="50 MB", retention="7 days", serialize=False)

# ── Strategy + engine imports ─────────────────────────────────────────────────
from src.data.polymarket_client import PolymarketClient
from src.data.price_feed        import BinancePriceFeed
from src.strategies.rebalancing  import RebalancingStrategy
from src.strategies.combinatorial import CombinatorialStrategy
from src.strategies.latency_arb  import LatencyArbStrategy
from src.strategies.market_making import MarketMakingStrategy
from src.execution.paper_engine  import PaperEngine
from src.utils.dashboard         import render_dashboard, console

# ── Config ────────────────────────────────────────────────────────────────────
ENABLE_REBALANCING    = os.getenv("ENABLE_REBALANCING",    "true").lower() == "true"
ENABLE_COMBINATORIAL  = os.getenv("ENABLE_COMBINATORIAL",  "true").lower() == "true"
ENABLE_LATENCY_ARB    = os.getenv("ENABLE_LATENCY_ARB",    "true").lower() == "true"
ENABLE_MARKET_MAKING  = os.getenv("ENABLE_MARKET_MAKING",  "true").lower() == "true"

SCAN_INTERVAL_SECONDS   = int(os.getenv("SCAN_INTERVAL_SECONDS",   "30"))
DASHBOARD_REFRESH       = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "5"))
MARKET_FETCH_INTERVAL   = int(os.getenv("MARKET_FETCH_INTERVAL",    "120"))
MAX_MARKETS_PER_SCAN    = int(os.getenv("MAX_MARKETS_PER_SCAN",     "200"))


class PolymarketArbBot:
    def __init__(self):
        self.engine        = PaperEngine()
        self.price_feed    = BinancePriceFeed()
        self.recent_opps:  List[dict] = []
        self._markets      = []
        self._running      = True
        self._market_lock  = asyncio.Lock()

    async def run(self) -> None:
        logger.info("=" * 60)
        logger.info("  POLYMARKET ARB BOT  —  PAPER TRADING MODE")
        logger.info("=" * 60)
        logger.info(f"  Starting balance: ${self.engine.portfolio.starting_cash:,.2f} USDC")
        logger.info(f"  Strategies: rebalancing={ENABLE_REBALANCING} "
                    f"combinatorial={ENABLE_COMBINATORIAL} "
                    f"latency={ENABLE_LATENCY_ARB} "
                    f"mm={ENABLE_MARKET_MAKING}")

        async with aiohttp.ClientSession() as session:
            client = PolymarketClient(session)

            # Build strategies
            strats = {}
            if ENABLE_REBALANCING:
                strats["rebalancing"] = RebalancingStrategy()
            if ENABLE_COMBINATORIAL:
                strats["combinatorial"] = CombinatorialStrategy()
            if ENABLE_LATENCY_ARB:
                strats["latency_arb"] = LatencyArbStrategy(self.price_feed)
            if ENABLE_MARKET_MAKING:
                strats["market_making"] = MarketMakingStrategy()

            # Start price feed in background
            feed_task = asyncio.create_task(self.price_feed.run())

            # Initial market fetch
            await self._refresh_markets(client, session)

            try:
                await asyncio.gather(
                    self._scan_loop(strats, client, session),
                    self._market_refresh_loop(client, session),
                    self._dashboard_loop(),
                    feed_task,
                )
            except asyncio.CancelledError:
                logger.info("Bot shutting down...")
            finally:
                feed_task.cancel()
                self._print_final_summary()

    # ── Loops ─────────────────────────────────────────────────────────────────

    async def _scan_loop(self, strats: dict, client: PolymarketClient, session) -> None:
        """Main strategy scanning loop."""
        while self._running:
            try:
                async with self._market_lock:
                    markets = list(self._markets[:MAX_MARKETS_PER_SCAN])

                if not markets:
                    logger.warning("[Scan] No markets loaded yet, waiting...")
                    await asyncio.sleep(10)
                    continue

                logger.debug(f"[Scan] Running strategies on {len(markets)} markets")

                if "rebalancing" in strats:
                    opps = strats["rebalancing"].scan(markets)
                    for opp in opps[:5]:   # limit trades per cycle
                        trade = self.engine.execute_rebalancing(opp)
                        if trade:
                            self._log_opp("rebalancing", opp.market.question, opp.profit_pct, True)

                if "combinatorial" in strats:
                    opps = strats["combinatorial"].scan(markets)
                    for opp in opps[:3]:
                        trade = self.engine.execute_combinatorial(opp)
                        if trade:
                            self._log_opp("combinatorial", opp.market_a.question, opp.profit_pct, True)

                if "latency_arb" in strats:
                    opps = strats["latency_arb"].scan(markets)
                    for opp in opps[:3]:
                        trade = self.engine.execute_latency_arb(opp)
                        if trade:
                            self._log_opp("latency_arb", opp.market.question, opp.lag_pct, True)

                if "market_making" in strats:
                    opps = strats["market_making"].scan(markets)
                    for opp in opps[:5]:
                        trade = self.engine.execute_market_making(opp)
                        if trade:
                            self._log_opp("market_making", opp.market.question, opp.spread_pct, True)

                # Write JSON stats checkpoint
                self._write_stats()

            except Exception as e:
                logger.error(f"[Scan] Error: {e}", exc_info=True)

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def _market_refresh_loop(self, client: PolymarketClient, session) -> None:
        """Refreshes market list every N seconds."""
        while self._running:
            await asyncio.sleep(MARKET_FETCH_INTERVAL)
            await self._refresh_markets(client, session)

    async def _dashboard_loop(self) -> None:
        """Refreshes terminal dashboard."""
        while self._running:
            try:
                render_dashboard(self.engine, self.recent_opps)
            except Exception as e:
                logger.debug(f"[Dashboard] Render error: {e}")
            await asyncio.sleep(DASHBOARD_REFRESH)

    async def _refresh_markets(self, client: PolymarketClient, session) -> None:
        logger.info("[Markets] Fetching active markets...")
        try:
            markets = await client.get_all_active_markets(max_pages=5)
            async with self._market_lock:
                self._markets = markets
            logger.info(f"[Markets] Loaded {len(markets)} active markets")
        except Exception as e:
            logger.error(f"[Markets] Refresh failed: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log_opp(self, type_: str, market: str, profit: float, traded: bool) -> None:
        self.recent_opps.append({
            "time":       datetime.utcnow().strftime("%H:%M:%S"),
            "type":       type_,
            "market":     market,
            "profit_pct": profit,
            "traded":     traded,
        })
        # Keep last 100
        self.recent_opps = self.recent_opps[-100:]

    def _write_stats(self) -> None:
        stats = self.engine.summary()
        stats["timestamp"] = datetime.utcnow().isoformat()
        Path("logs/stats.json").write_text(json.dumps(stats, indent=2))

    def _print_final_summary(self) -> None:
        s = self.engine.summary()
        console.print("\n[bold cyan]══ FINAL SUMMARY ══[/bold cyan]")
        console.print(f"  Balance:      ${s['balance']:,.2f}")
        console.print(f"  Total PnL:    {'+'if s['total_pnl']>=0 else ''}{s['total_pnl']:,.2f}")
        console.print(f"  Return:       {s['return_pct']:+.2f}%")
        console.print(f"  Total Trades: {s['total_trades']}")
        console.print(f"  Win Rate:     {s['win_rate']}%")
        for strat, stats in s["strategy_stats"].items():
            if stats["trades"] > 0:
                console.print(f"  {strat:<16} trades={stats['trades']} pnl={stats['pnl']:+.2f}")


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    bot = PolymarketArbBot()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: setattr(bot, "_running", False))

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
