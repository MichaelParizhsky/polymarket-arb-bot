#!/usr/bin/env python3
"""
Polymarket Arbitrage Bot
========================
Four strategies: market rebalancing, combinatorial arb,
latency arb vs Binance, and market making.

Usage:
    python main.py                # run bot (paper trading by default)
    python main.py --live         # live trading (requires API keys)
    python main.py --summary      # print portfolio summary and exit
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Any

import uvicorn
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel

from config import CONFIG
from src.dashboard.app import app as dashboard_app, register as dashboard_register
from src.exchange.polymarket import PolymarketClient
from src.exchange.binance import BinanceFeed
from src.portfolio.paper_trading import PaperPortfolio
from src.risk.risk_manager import RiskManager
from src.exchange.polymarket_ws import PolymarketWSFeed
from src.exchange.kalshi import KalshiClient
from src.strategies.rebalancing import RebalancingStrategy
from src.strategies.combinatorial import CombinatorialStrategy
from src.strategies.latency_arb import LatencyArbStrategy
from src.strategies.market_making import MarketMakingStrategy
from src.strategies.resolution import ResolutionStrategy
from src.strategies.event_driven import EventDrivenStrategy
from src.strategies.cross_exchange import CrossExchangeStrategy
from src.strategies.futures_hedge import FuturesHedge
from src.utils.logger import logger, setup_logger
from src.utils.metrics import start_metrics_server, trades_total, arb_executed
from src.meta_agent.analyzer import PortfolioSnapshot

console = Console()

# Ensure logs directory exists
os.makedirs("logs", exist_ok=True)


class ArbBot:
    def __init__(self) -> None:
        setup_logger(CONFIG.log_level)
        self.config = CONFIG
        self.paper = self.config.paper_trading

        # Core components
        self.portfolio = PaperPortfolio(starting_balance=self.config.starting_balance)
        self.risk = RiskManager(config=self.config, portfolio=self.portfolio)
        self.binance = BinanceFeed(reconnect_delay=self.config.binance_ws_reconnect_delay)

        self.poly: PolymarketClient | None = None
        self._ws_feed: PolymarketWSFeed | None = None
        self._kalshi: KalshiClient | None = None
        self._futures_hedge: FuturesHedge | None = None

        # Strategies
        self._strategies = []
        self._running = False
        self._cycle_count = 0
        self._start_time = time.time()

    def _build_strategies(self) -> None:
        cfg = self.config.strategies
        if cfg.rebalancing_enabled:
            self._strategies.append(
                RebalancingStrategy(self.config, self.portfolio, self.risk)
            )
        if cfg.combinatorial_enabled:
            self._strategies.append(
                CombinatorialStrategy(self.config, self.portfolio, self.risk)
            )
        if cfg.latency_arb_enabled:
            self._strategies.append(
                LatencyArbStrategy(self.config, self.portfolio, self.risk, self.binance)
            )
        if cfg.market_making_enabled:
            self._strategies.append(
                MarketMakingStrategy(self.config, self.portfolio, self.risk)
            )
        if cfg.resolution_enabled:
            self._strategies.append(
                ResolutionStrategy(self.config, self.portfolio, self.risk)
            )
        if cfg.event_driven_enabled:
            self._strategies.append(
                EventDrivenStrategy(self.config, self.portfolio, self.risk)
            )
        if cfg.cross_exchange_enabled and self._kalshi:
            self._strategies.append(
                CrossExchangeStrategy(self.config, self.portfolio, self.risk, self._kalshi)
            )
        logger.info(f"Loaded {len(self._strategies)} strategies: "
                    f"{[s.name for s in self._strategies]}")

    async def run(self) -> None:
        """Main bot loop."""
        mode = "PAPER TRADING" if self.paper else "LIVE TRADING"
        logger.info(f"Starting Polymarket Arb Bot [{mode}]")
        logger.info(f"Starting balance: ${self.config.starting_balance:,.2f} USDC")

        if not self.paper:
            _warn_live_trading()

        # Start metrics server
        try:
            start_metrics_server(self.config.metrics_port)
            logger.info(f"Metrics server: http://0.0.0.0:{self.config.metrics_port}")
        except Exception as exc:
            logger.warning(f"Metrics server failed to start: {exc}")

        # Initialise optional components
        if self.config.strategies.use_ws_orderbook:
            self._ws_feed = PolymarketWSFeed()
            logger.info("Polymarket WebSocket orderbook feed enabled")

        if self.config.kalshi.enabled:
            self._kalshi = KalshiClient(self.config.kalshi, paper_trading=self.paper)
            logger.info("Kalshi client enabled")

        if self.config.binance.futures_enabled:
            self._futures_hedge = FuturesHedge(self.config, paper_trading=self.paper)
            logger.info("Binance futures hedging enabled")

        self._build_strategies()
        self._running = True

        # Register portfolio with dashboard
        dashboard_register(self.portfolio, self._start_time)

        # Start dashboard server
        dashboard_port = 5000
        for _port in range(5000, 5010):
            import socket as _sock
            with _sock.socket() as s:
                if s.connect_ex(("127.0.0.1", _port)) != 0:
                    dashboard_port = _port
                    break
        dashboard_config = uvicorn.Config(
            dashboard_app, host="0.0.0.0", port=dashboard_port, log_level="warning"
        )
        dashboard_server = uvicorn.Server(dashboard_config)
        asyncio.create_task(dashboard_server.serve())
        logger.info(f"Dashboard: http://localhost:{dashboard_port}")

        # Start Binance feed
        await self.binance.fetch_snapshot()   # initial REST snapshot
        await self.binance.start()

        # Start Polymarket WS orderbook feed if enabled
        if self._ws_feed:
            await self._ws_feed.start()

        async with PolymarketClient(self.config.polymarket, paper_trading=self.paper) as poly:
            self.poly = poly

            # Register shutdown handlers
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self._handle_shutdown)
                except NotImplementedError:
                    pass  # Windows doesn't support add_signal_handler

            # Start meta-agent background task
            asyncio.create_task(self._meta_agent_loop())

            try:
                await self._main_loop()
            finally:
                await self._shutdown()

    async def _main_loop(self) -> None:
        """Core scanning loop."""
        logger.info("Bot running. Press Ctrl+C to stop.")
        last_summary_at = time.time()
        summary_interval = 300   # print summary every 5 minutes

        while self._running:
            cycle_start = time.perf_counter()
            self._cycle_count += 1
            from src.dashboard import app as _dash_mod
            import src.dashboard.app as _dash_mod
            _dash_mod._cycle_count = self._cycle_count

            try:
                context = await self._build_context()

                if not context.get("markets"):
                    logger.warning("No markets loaded, retrying...")
                    await asyncio.sleep(5)
                    continue

                # Run all strategies in parallel
                all_signals = []
                strategy_tasks = [s.scan(context) for s in self._strategies]
                results = await asyncio.gather(*strategy_tasks, return_exceptions=True)

                for strat, result in zip(self._strategies, results):
                    if isinstance(result, Exception):
                        logger.error(f"Strategy {strat.name} error: {result}")
                    else:
                        all_signals.extend(result)

                # Execute signals through risk manager
                if all_signals:
                    await self._execute_signals(all_signals, context)

                # Periodic summary + state save
                now = time.time()
                if now - last_summary_at > summary_interval:
                    price_map = self._build_price_map(context)
                    console.print(self.portfolio.summary(price_map))
                    self.portfolio.save_to_json()
                    last_summary_at = now

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Main loop error: {exc}", exc_info=True)

            elapsed = time.perf_counter() - cycle_start
            sleep_time = max(0, self.config.market_poll_interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def _build_context(self) -> dict[str, Any]:
        """Fetch all market data needed by strategies."""
        markets = await self.poly.get_markets_cached()

        # Limit to top N markets by volume
        max_markets = self.config.strategies.max_markets
        markets_sorted = sorted(markets, key=lambda m: m.volume, reverse=True)[:max_markets]

        # Collect all token IDs
        token_ids = []
        for m in markets_sorted:
            for t in m.tokens:
                token_ids.append(t.token_id)

        # Keep WS feed subscribed to current token set
        if self._ws_feed:
            self._ws_feed.subscribe(token_ids)

        # Use WebSocket cache if available, fall back to REST
        orderbooks: dict[str, Any] = {}
        if self.config.strategies.use_ws_orderbook and self._ws_feed:
            for tid in token_ids:
                ob = self._ws_feed.get_orderbook(tid)
                if ob:
                    orderbooks[tid] = ob
            # REST fill for any missing
            missing = [tid for tid in token_ids if tid not in orderbooks]
        else:
            missing = token_ids

        # REST fetch for missing orderbooks
        batch_size = 30
        for i in range(0, len(missing), batch_size):
            batch = missing[i:i + batch_size]
            tasks = [self.poly.get_orderbook(tid) for tid in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for tid, res in zip(batch, results):
                if not isinstance(res, Exception):
                    orderbooks[tid] = res

        # Fetch Kalshi markets if enabled
        kalshi_markets = []
        if self._kalshi and self.config.kalshi.enabled:
            try:
                kalshi_markets = await self._kalshi.get_markets_cached()
            except Exception as exc:
                logger.debug(f"Kalshi markets fetch failed: {exc}")

        return {
            "markets": markets_sorted,
            "orderbooks": orderbooks,
            "binance_feed": self.binance,
            "kalshi_markets": kalshi_markets,
            "timestamp": time.time(),
        }

    async def _execute_signals(
        self, signals: list, context: dict[str, Any]
    ) -> None:
        """Execute trading signals with risk checks."""
        # Deduplicate: same token_id + side should only execute once per cycle
        seen: set[tuple[str, str]] = set()

        for sig in signals:
            key = (sig.token_id, sig.side)
            if key in seen:
                continue
            seen.add(key)

            if self.risk.is_hard_stopped():
                logger.critical("Hard stop active — no new trades")
                break

            ok, reason = self.risk.check_trade(
                sig.token_id, sig.side, sig.size_usdc, sig.strategy
            )
            if not ok:
                logger.debug(f"Signal rejected [{sig.strategy}] {sig.side} {sig.token_id[:16]}: {reason}")
                continue

            # Execute via portfolio
            contracts = sig.size_usdc / max(sig.price, 0.001)
            if sig.side == "BUY":
                # Find market context for this token
                market_question, outcome = self._find_market_info(sig.token_id, context)
                trade = self.portfolio.buy(
                    token_id=sig.token_id,
                    contracts=contracts,
                    price=sig.price,
                    strategy=sig.strategy,
                    market_question=market_question,
                    outcome=outcome,
                    notes=sig.notes,
                )
            else:
                trade = self.portfolio.sell(
                    token_id=sig.token_id,
                    contracts=contracts,
                    price=sig.price,
                    strategy=sig.strategy,
                    notes=sig.notes,
                )

            if trade:
                trades_total.labels(strategy=sig.strategy, side=sig.side).inc()
                arb_executed.labels(strategy=sig.strategy).inc()

    def _find_market_info(
        self, token_id: str, context: dict[str, Any]
    ) -> tuple[str, str]:
        for m in context.get("markets", []):
            for t in m.tokens:
                if t.token_id == token_id:
                    return m.question, t.outcome
        return "", ""

    def _build_price_map(self, context: dict[str, Any]) -> dict[str, float]:
        price_map: dict[str, float] = {}
        for tid, book in context.get("orderbooks", {}).items():
            if book.mid is not None:
                price_map[tid] = book.mid
        return price_map

    async def _meta_agent_loop(self) -> None:
        """Run Claude meta-agent analysis every 30 minutes."""
        import json
        import re
        import anthropic

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            logger.info("Meta-agent disabled (no ANTHROPIC_API_KEY)")
            return

        interval = int(os.getenv("META_AGENT_INTERVAL_MINUTES", "30")) * 60
        min_trades = 10

        logger.info(f"Meta-agent started — analyzing every {interval//60} minutes")

        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break

            try:
                state_path = "logs/portfolio_state.json"
                if not os.path.exists(state_path):
                    logger.debug("Meta-agent: no portfolio state yet, skipping")
                    continue

                snapshot = PortfolioSnapshot.from_json(state_path)
                if len(snapshot.trades) < min_trades:
                    logger.debug(f"Meta-agent: only {len(snapshot.trades)} trades, need {min_trades}")
                    continue

                analysis_data = snapshot.to_analysis_dict()
                current_env = {k: os.getenv(k, "") for k in [
                    "MIN_EDGE_THRESHOLD", "MAX_POSITION_SIZE", "MAX_TOTAL_EXPOSURE",
                    "MAX_SLIPPAGE", "MM_SPREAD_BPS", "MM_ORDER_SIZE", "MM_MAX_INVENTORY",
                    "STRATEGY_REBALANCING", "STRATEGY_COMBINATORIAL",
                    "STRATEGY_LATENCY_ARB", "STRATEGY_MARKET_MAKING",
                ]}

                logger.info("Meta-agent: calling Claude for performance analysis...")
                client = anthropic.AsyncAnthropic(api_key=api_key)

                system = (
                    "You are an expert quantitative analyst for prediction market arbitrage. "
                    "Analyze trading bot performance and suggest conservative parameter improvements. "
                    "Return a brief analysis (2-3 paragraphs) then a JSON block with ONLY keys to change, "
                    "wrapped in ```json ... ```. Be conservative — small adjustments only."
                )
                user_msg = (
                    f"Analyze this Polymarket arb bot performance:\n\n"
                    f"Config:\n```json\n{json.dumps(current_env, indent=2)}\n```\n\n"
                    f"Performance:\n```json\n{json.dumps(analysis_data, indent=2)}\n```\n\n"
                    "Which strategies are profitable? Should any thresholds change? "
                    "Suggest only small, safe adjustments."
                )

                response = await client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=1500,
                    thinking={"type": "adaptive"},
                    system=system,
                    messages=[{"role": "user", "content": user_msg}],
                )
                text = next((b.text for b in response.content if hasattr(b, "text")), "")

                # Extract proposed JSON changes
                proposed = {}
                match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
                if match:
                    try:
                        proposed = json.loads(match.group(1))
                    except json.JSONDecodeError:
                        pass

                log_entry = {
                    "timestamp": time.time(),
                    "analysis": text,
                    "proposed_changes": proposed,
                    "current_values": current_env,
                    "portfolio_snapshot": analysis_data,
                }
                os.makedirs("logs", exist_ok=True)
                log_path = f"logs/meta_agent_{int(time.time())}.json"
                with open(log_path, "w") as f:
                    json.dump(log_entry, f, indent=2)

                logger.info(f"Meta-agent: analysis complete — {len(proposed)} suggestion(s) saved to dashboard")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Meta-agent error: {exc}")

    def _handle_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self._running = False

    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")
        await self.binance.stop()
        if self._ws_feed:
            await self._ws_feed.stop()
        uptime = time.time() - self._start_time
        logger.info(f"Uptime: {uptime:.0f}s | Cycles: {self._cycle_count}")
        console.print(self.portfolio.summary())
        logger.info("Bot stopped")


def _warn_live_trading() -> None:
    console.print(Panel(
        "[bold red]WARNING: LIVE TRADING MODE[/bold red]\n"
        "Real USDC will be spent. Ensure your API keys are correct and\n"
        "you understand the risks before proceeding.",
        title="Live Trading",
        border_style="red",
    ))


async def print_summary() -> None:
    """Print portfolio summary and exit."""
    portfolio = PaperPortfolio(starting_balance=CONFIG.starting_balance)
    console.print(portfolio.summary())


def main() -> None:
    if "--summary" in sys.argv:
        asyncio.run(print_summary())
        return

    if "--live" in sys.argv:
        CONFIG.paper_trading = False

    bot = ArbBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
