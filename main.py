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

from rich.console import Console
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
from src.strategies.market_making import MarketMakingStrategy
from src.strategies.resolution import ResolutionStrategy
from src.strategies.event_driven import EventDrivenStrategy
from src.strategies.cross_exchange import CrossExchangeStrategy
from src.strategies.futures_hedge import FuturesHedge
from src.strategies.quick_resolution import QuickResolutionStrategy
from src.strategies.ensemble import EnsembleStrategy
from src.utils.hedge_manager import HedgeManager
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

        try:
            from src.utils.database import init_db
            init_db()
        except ImportError:
            logger.debug("src.utils.database not available — skipping init_db()")

        # Core components
        self.portfolio = PaperPortfolio(starting_balance=self.config.starting_balance)
        self.portfolio.load_from_json()  # restore state from previous run if available
        self.risk = RiskManager(config=self.config, portfolio=self.portfolio)
        self.binance = BinanceFeed(reconnect_delay=self.config.binance_ws_reconnect_delay)

        self.poly: PolymarketClient | None = None
        self._ws_feed: PolymarketWSFeed | None = None
        self._kalshi: KalshiClient | None = None
        self._futures_hedge: FuturesHedge | None = None
        self._hedge_manager: HedgeManager | None = None
        self._news_monitor = None  # set in run() if NewsMonitor available
        self._ensemble_strategy = None  # set in _build_strategies() if enabled

        # Strategies
        self._strategies = []
        self._running = False
        self._cycle_count = 0
        self._start_time = time.time()
        # Rate limiting
        self._last_trade_time: float = 0.0
        self._token_last_traded: dict[str, float] = {}
        self._dynamic_strategies: list = []  # strategies deployed at runtime via dashboard

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
        if getattr(cfg, 'market_making_enabled', False):
            self._strategies.append(
                MarketMakingStrategy(self.config, self.portfolio, self.risk)
            )
            logger.info("Market making strategy ENABLED")
        else:
            logger.warning(
                "Market making strategy DISABLED (adverse selection risk — "
                "enable with STRATEGY_MARKET_MAKING_ENABLED=true)"
            )
        if cfg.resolution_enabled:
            self._strategies.append(
                ResolutionStrategy(self.config, self.portfolio, self.risk)
            )
        if cfg.event_driven_enabled:
            self._strategies.append(
                EventDrivenStrategy(self.config, self.portfolio, self.risk,
                                    news_monitor=self._news_monitor)
            )
        if cfg.cross_exchange_enabled and self._kalshi:
            self._strategies.append(
                CrossExchangeStrategy(self.config, self.portfolio, self.risk, self._kalshi)
            )
        if cfg.quick_resolution_enabled:
            self._strategies.append(
                QuickResolutionStrategy(self.config, self.portfolio, self.risk)
            )
        # EnsembleStrategy disabled: LLMs lack calibration for real-time probability
        # estimation. Replace with a properly trained ML model before re-enabling.
        # ensemble = EnsembleStrategy(...)
        # self.strategies.append(ensemble)
        if False and cfg.ensemble_enabled:  # noqa: SIM210 — intentionally disabled
            self._ensemble_strategy = EnsembleStrategy(self.config, self.portfolio, self.risk)
            self._strategies.append(self._ensemble_strategy)
        strategy_names = [s.name for s in self._strategies]
        logger.info(f"Loaded {len(self._strategies)} strategies: {strategy_names}")
        # Log which strategies are disabled so Railway logs show full picture
        all_flags = {
            "RebalancingStrategy": cfg.rebalancing_enabled,
            "CombinatorialStrategy": cfg.combinatorial_enabled,
            "MarketMakingStrategy": cfg.market_making_enabled,
            "ResolutionStrategy": cfg.resolution_enabled,
            "EventDrivenStrategy": cfg.event_driven_enabled,
            "CrossExchangeStrategy": cfg.cross_exchange_enabled,
            "QuickResolutionStrategy": cfg.quick_resolution_enabled,
            "EnsembleStrategy": cfg.ensemble_enabled,
        }
        disabled = [k for k, v in all_flags.items() if not v]
        if disabled:
            logger.info(f"Disabled strategies: {disabled}")
        # Re-add any runtime-deployed strategies
        self._strategies.extend(self._dynamic_strategies)

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

        if self._futures_hedge is not None:
            self._hedge_manager = HedgeManager(
                futures_hedge=self._futures_hedge,
                portfolio=self.portfolio,
                config=self.config,
            )
            logger.info("HedgeManager initialised")

        # Start news monitor if available
        try:
            from src.exchange.news_monitor import NewsMonitor
            self._news_monitor = NewsMonitor()
            await self._news_monitor.start()
            logger.info("News monitor started")
        except Exception as exc:
            logger.info(f"News monitor not available: {exc}")

        self._build_strategies()
        self._running = True

        # Register portfolio with dashboard
        dashboard_register(self.portfolio, self._start_time,
                           config=self.config, risk=self.risk,
                           binance=self.binance, kalshi=self._kalshi,
                           news_monitor=self._news_monitor,
                           hedge_manager=self._hedge_manager,
                           ensemble_strategy=self._ensemble_strategy)

        # Start dashboard server in a dedicated thread with its own event loop.
        # This isolates the dashboard from the trading bot's asyncio loop so that
        # heavy scanning / API calls never starve the HTTP server.
        import threading
        dashboard_port = 5000
        for _port in range(5000, 5010):
            import socket as _sock
            with _sock.socket() as s:
                if s.connect_ex(("127.0.0.1", _port)) != 0:
                    dashboard_port = _port
                    break

        def _run_dashboard(port: int) -> None:
            import asyncio as _asyncio
            import uvicorn as _uvi
            _loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(_loop)
            _cfg = _uvi.Config(dashboard_app, host="0.0.0.0", port=port, log_level="warning")
            _srv = _uvi.Server(_cfg)
            _srv.install_signal_handlers = lambda: None  # signal handlers only work in main thread
            _loop.run_until_complete(_srv.serve())

        _dash_thread = threading.Thread(target=_run_dashboard, args=(dashboard_port,), daemon=True)
        _dash_thread.start()
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

            # Clean up old log bloat from previous deploys
            self._startup_cleanup()

            # Apply any previously auto-tuned config
            self._load_saved_config()

            # Start background tasks
            asyncio.create_task(self._meta_agent_loop())
            asyncio.create_task(self._auto_close_resolved_loop())

            try:
                await self._main_loop()
            finally:
                await self._shutdown()

    async def _main_loop(self) -> None:
        """Core scanning loop."""
        logger.info("Bot running. Press Ctrl+C to stop.")
        last_summary_at = time.time()
        summary_interval = 60   # save state every 60s (was 300s — reduces data loss on Railway redeploy)

        while self._running:
            # Check for dashboard-deployed strategies every 10 cycles
            if self._cycle_count % 10 == 0:
                self._process_pending_deploys()
            cycle_start = time.perf_counter()
            self._cycle_count += 1
            import src.dashboard.app as _dash_mod
            _dash_mod._cycle_count = self._cycle_count

            try:
                context = await self._build_context()

                if not context.get("markets"):
                    logger.warning("No markets loaded, retrying...")
                    await asyncio.sleep(5)
                    continue

                # Run all strategies concurrently
                results = await asyncio.gather(
                    *[strategy.scan(context) for strategy in self._strategies],
                    return_exceptions=True
                )

                all_signals = []
                for strategy, result in zip(self._strategies, results):
                    if isinstance(result, Exception):
                        logger.error(f"Strategy {strategy.__class__.__name__} scan failed: {result}")
                        continue
                    all_signals.extend(result or [])

                # Filter signals from strategies that have exceeded their loss budget
                disabled_by_budget = set()
                strategy_pnl_map = self.portfolio.strategy_pnl()
                for strat_name, budget in self.config.risk.strategy_loss_budget.items():
                    strategy_pnl = strategy_pnl_map.get(strat_name, 0.0)
                    if strategy_pnl < -budget:
                        disabled_by_budget.add(strat_name)
                        logger.warning(
                            f"Strategy {strat_name} disabled: loss budget ${budget:.0f} exceeded "
                            f"(current: ${strategy_pnl:.2f})"
                        )

                if disabled_by_budget:
                    all_signals = [s for s in all_signals if s.strategy not in disabled_by_budget]

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
        expiring = await self.poly.get_expiring_markets_cached(max_hours=48.0)

        # Merge: expiring markets lead the list so they survive the volume sort cap.
        # Deduplicate by condition_id; expiring entries take priority.
        seen_ids = {m.condition_id for m in expiring}
        markets = list(expiring) + [m for m in markets if m.condition_id not in seen_ids]

        # Filter to markets resolving within MAX_DAYS_TO_RESOLUTION
        max_days = self.config.strategies.max_days_to_resolution
        if max_days > 0:
            from datetime import datetime, timezone
            cutoff = datetime.now(timezone.utc).timestamp() + max_days * 86400
            filtered = []
            for m in markets:
                if not m.end_date_iso:
                    continue  # skip markets with no resolution date
                try:
                    end_ts = datetime.fromisoformat(
                        m.end_date_iso.replace("Z", "+00:00")
                    ).timestamp()
                    if end_ts <= cutoff:
                        filtered.append(m)
                except ValueError:
                    continue
            markets = filtered
            logger.debug(f"Date filter ({max_days}d): {len(markets)} markets within window")

        # Limit to top N markets by volume, but always keep expiring markets at the front.
        # Expiring markets have low lifetime volume and would otherwise be sorted out.
        max_markets = self.config.strategies.max_markets
        expiring_ids = {m.condition_id for m in expiring}
        expiring_in_list = [m for m in markets if m.condition_id in expiring_ids]
        general_markets = [m for m in markets if m.condition_id not in expiring_ids]
        general_sorted = sorted(general_markets, key=lambda m: m.volume, reverse=True)
        remaining_slots = max(0, max_markets - len(expiring_in_list))
        markets_sorted = expiring_in_list + general_sorted[:remaining_slots]

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
        # Track tokens whose paired leg was skipped so we don't execute one side alone.
        # A rebalancing arb requires both YES and NO to fill — executing only one leg
        # creates an unhedged directional position instead of a risk-free arb.
        skipped_pairs: set[str] = set()

        now = time.time()
        min_interval = self.config.risk.min_trade_interval
        token_cooldown = self.config.risk.token_cooldown

        # Identify paired signals (rebalancing YES+NO pairs).
        # If rate budget can't cover even 1 trade, skip BOTH legs to avoid an unhedged position.
        pair_groups: dict[str, list] = {}
        for sig in signals:
            pair_id = (sig.metadata or {}).get("pair_token_id")
            if pair_id:
                key = "_".join(sorted([sig.token_id, pair_id]))
                pair_groups.setdefault(key, []).append(sig)

        skip_token_ids: set[str] = set()
        since_last = now - self._last_trade_time
        for key, legs in pair_groups.items():
            if len(legs) >= 2 and since_last < min_interval:
                # Not enough rate budget for even 1 trade — skip both legs
                for leg in legs:
                    skip_token_ids.add(leg.token_id)

        for sig in signals:
            if sig.token_id in skip_token_ids:
                logger.debug(f"Skipping paired signal for {sig.token_id[:12]} — insufficient rate budget")
                continue

            key = (sig.token_id, sig.side)
            if key in seen:
                continue
            seen.add(key)

            if self.risk.is_hard_stopped():
                logger.critical("Hard stop active — no new trades")
                break

            # Skip if this signal's paired leg was already skipped
            if sig.token_id in skipped_pairs:
                logger.debug(f"Skipping {sig.token_id[:16]} — paired leg was skipped")
                continue

            # Global rate limit: enforce minimum gap between any two trades
            since_last = now - self._last_trade_time
            if since_last < min_interval:
                logger.debug(f"Rate limit: {since_last:.0f}s since last trade (need {min_interval}s)")
                # Mark the paired leg as skipped too so we don't execute half the arb
                pair_id = (sig.metadata or {}).get("pair_token_id")
                if pair_id:
                    skipped_pairs.add(pair_id)
                continue  # don't break — other unrelated signals may still be valid

            # Per-token cooldown: don't hammer the same market
            token_last = self._token_last_traded.get(sig.token_id, 0)
            if now - token_last < token_cooldown:
                logger.debug(f"Token cooldown: {sig.token_id[:16]} traded {now-token_last:.0f}s ago")
                pair_id = (sig.metadata or {}).get("pair_token_id")
                if pair_id:
                    skipped_pairs.add(pair_id)
                continue

            ok, reason = self.risk.check_trade(
                sig.token_id, sig.side, sig.size_usdc, sig.strategy
            )
            if not ok:
                logger.debug(f"Signal rejected [{sig.strategy}] {sig.side} {sig.token_id[:16]}: {reason}")
                pair_id = (sig.metadata or {}).get("pair_token_id")
                if pair_id:
                    skipped_pairs.add(pair_id)
                continue

            # Execute via portfolio (paper) or live order (live)
            contracts = sig.size_usdc / max(sig.price, 0.001)
            trade = None

            if not self.paper and sig.side == "BUY":
                # Live: place limit order and handle immediate FILLED result
                result = await self.poly.place_limit_order(
                    token_id=sig.token_id,
                    side=sig.side,
                    price=sig.price,
                    size=contracts,
                )
                if result and result.status == "FILLED":
                    market_question, outcome = self._find_market_info(sig.token_id, context)
                    fill_contracts = (
                        result.filled_size
                        if hasattr(result, "filled_size") and result.filled_size
                        else contracts
                    )
                    fill_price = (
                        result.avg_fill_price
                        if hasattr(result, "avg_fill_price") and result.avg_fill_price
                        else sig.price
                    )
                    trade = self.portfolio.buy(
                        token_id=sig.token_id,
                        contracts=fill_contracts,
                        price=fill_price,
                        strategy=sig.strategy,
                        market_question=market_question,
                        outcome=outcome,
                        notes=sig.notes,
                    )
                    if trade:
                        trades_total.labels(strategy=sig.strategy, side=sig.side).inc()
                        arb_executed.labels(strategy=sig.strategy).inc()
                        self._last_trade_time = time.time()
                        self._token_last_traded[sig.token_id] = time.time()
                        self.portfolio.save_to_json()
            elif sig.side == "BUY":
                # Paper: simulate directly
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

            if trade and not (not self.paper and sig.side == "BUY"):
                # For paper BUY and all SELL paths: increment metrics here
                # (live BUY path already incremented above when result.status == "FILLED")
                trades_total.labels(strategy=sig.strategy, side=sig.side).inc()
                arb_executed.labels(strategy=sig.strategy).inc()
                self._last_trade_time = time.time()
                self._token_last_traded[sig.token_id] = time.time()
                self.portfolio.save_to_json()

            if trade and sig.side == "BUY" and self._hedge_manager is not None:
                # Auto-hedge crypto BUY positions via Binance perpetual futures
                market_question, _ = self._find_market_info(sig.token_id, context)
                market_tags = self._find_market_tags(sig.token_id, context)
                await self._hedge_manager.maybe_open_hedge(
                    token_id=sig.token_id,
                    market_question=market_question,
                    market_tags=market_tags,
                    side=sig.side,
                    size_usdc=sig.size_usdc,
                )

    def _find_market_info(
        self, token_id: str, context: dict[str, Any]
    ) -> tuple[str, str]:
        for m in context.get("markets", []):
            for t in m.tokens:
                if t.token_id == token_id:
                    return m.question, t.outcome
        return "", ""

    def _find_market_tags(
        self, token_id: str, context: dict[str, Any]
    ) -> list[str]:
        for m in context.get("markets", []):
            for t in m.tokens:
                if t.token_id == token_id:
                    return list(getattr(m, "tags", []))
        return []

    def _build_price_map(self, context: dict[str, Any]) -> dict[str, float]:
        price_map: dict[str, float] = {}
        for tid, book in context.get("orderbooks", {}).items():
            if book.mid is not None:
                price_map[tid] = book.mid
        return price_map

    async def _auto_close_resolved_loop(self) -> None:
        """
        Periodically scan open positions for markets that have resolved.
        A resolved binary market pays $1.00 for winning tokens and $0.00 for losing ones.
        We detect resolution when the best_bid >= 0.995 (winner) or best_ask <= 0.005 (loser)
        and close the position at that price.
        """
        check_interval = 30  # seconds between resolution checks

        while self._running:
            await asyncio.sleep(check_interval)
            if not self._running or not self.poly:
                continue

            try:
                open_positions = {
                    tid: pos for tid, pos in self.portfolio.positions.items()
                    if pos.contracts > 0
                }
                if not open_positions:
                    continue

                for token_id, pos in list(open_positions.items()):
                    try:
                        book = await self.poly.get_orderbook(token_id)
                    except Exception:
                        continue

                    # Winning resolution: bid >= 0.995 — sell at near $1.00
                    if book.best_bid is not None and book.best_bid >= 0.995:
                        trade = self.portfolio.sell(
                            token_id=token_id,
                            contracts=pos.contracts,
                            price=book.best_bid,
                            strategy="auto_close",
                            notes=f"[AUTO-CLOSE] resolved winner @ {book.best_bid:.4f}",
                        )
                        if trade:
                            logger.info(
                                f"[AUTO-CLOSE] Closed resolved winner {token_id[:16]}... "
                                f"@ {book.best_bid:.4f} | {pos.contracts:.2f} contracts"
                            )
                            if self._hedge_manager is not None:
                                await self._hedge_manager.maybe_close_hedge(token_id)
                    # Losing resolution: ask <= 0.005 — position is worthless, sell at market
                    elif book.best_ask is not None and book.best_ask <= 0.005:
                        trade = self.portfolio.sell(
                            token_id=token_id,
                            contracts=pos.contracts,
                            price=book.best_ask or 0.001,
                            strategy="auto_close",
                            notes=f"[AUTO-CLOSE] resolved loser @ {book.best_ask:.4f}",
                        )
                        if trade:
                            logger.info(
                                f"[AUTO-CLOSE] Closed resolved loser {token_id[:16]}... "
                                f"@ {book.best_ask:.4f} | loss={pos.cost_basis:.2f} USDC"
                            )
                            if self._hedge_manager is not None:
                                await self._hedge_manager.maybe_close_hedge(token_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Auto-close loop error: {exc}")

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
        min_trades = 5   # lowered from 20 — Railway redeploys wipe state so 20 is rarely reached

        logger.info(f"Meta-agent started — first analysis in {interval//60} minutes")

        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break

            try:
                # Use live portfolio data directly (no file dependency)
                self.portfolio.save_to_json()
                state_path = "logs/portfolio_state.json"
                if not os.path.exists(state_path):
                    logger.info("Meta-agent: no portfolio state yet, skipping this cycle")
                    continue

                snapshot = PortfolioSnapshot.from_json(state_path)
                # Always write a heartbeat so we can tell the meta-agent loop is alive
                heartbeat = {
                    "alive": True,
                    "timestamp": time.time(),
                    "trades": len(snapshot.trades),
                    "min_trades_needed": min_trades,
                    "cycle_count": self._cycle_count,
                }
                with open("logs/meta_agent_heartbeat.json", "w") as _hf:
                    json.dump(heartbeat, _hf)

                if len(snapshot.trades) < min_trades:
                    logger.info(f"Meta-agent: only {len(snapshot.trades)} trades so far (need {min_trades}), skipping analysis")
                    continue

                analysis_data = snapshot.to_analysis_dict()
                current_env = {k: str(getattr(
                    self.config.risk if v[0] == "risk" else self.config.strategies,
                    v[1]
                )) for k, v in self._CONFIG_MAP.items()}

                logger.info("Meta-agent: calling Claude for performance analysis...")
                client = anthropic.AsyncAnthropic(api_key=api_key)

                # Include live risk health in the analysis
                live_health = self.risk.portfolio_health_score()

                system = (
                    "You are an expert quantitative analyst for a Polymarket/Kalshi prediction market "
                    "arbitrage bot. Your role combines four perspectives:\n\n"

                    "1. QUANT ANALYST: Evaluate each strategy as a revenue line. "
                    "Think in terms of ROI (PnL / volume traded), not just raw PnL. "
                    "A strategy trading $10k volume for $50 PnL (0.5% ROI) is better than one "
                    "trading $1k for $30 (3% ROI) only if it scales — check trades_per_hour.\n\n"

                    "2. RISK MANAGER: Use the health score as your primary triage signal. "
                    "CRITICAL (<25) or WEAK (<50) health requires defensive action first — "
                    "widen thresholds, reduce exposure — before optimizing returns. "
                    "HEALTHY (>75) health permits more aggressive parameter tuning. "
                    "Never raise MAX_TOTAL_EXPOSURE when drawdown is above 8%. "
                    "Never lower MIN_EDGE_THRESHOLD when win_rate is below 45%.\n\n"

                    "3. SCENARIO THINKER: Before recommending any change, consider its failure mode. "
                    "If you raise MM_SPREAD_BPS, does inventory risk increase? "
                    "If you disable a strategy, does it leave a gap in market coverage? "
                    "Prefer reversible changes. Prefer changes with evidence from >= 10 trades.\n\n"

                    "4. DECISION LOGGER: Your JSON output is stored and audited. "
                    "Every non-empty change must be justified by data in the performance snapshot. "
                    "Do not make changes based on gut feel or recency bias from a single bad cycle.\n\n"

                    "PARAMETERS YOU CAN CHANGE:\n"
                    "  Risk: MIN_EDGE_THRESHOLD, MAX_POSITION_SIZE, MAX_TOTAL_EXPOSURE, "
                    "MAX_SLIPPAGE, MIN_TRADE_INTERVAL, TOKEN_COOLDOWN\n"
                    "  Market making: MM_SPREAD_BPS, MM_ORDER_SIZE, MM_MAX_INVENTORY\n"
                    "  Coverage: MAX_DAYS_TO_RESOLUTION, MAX_MARKETS\n"
                    "  Strategies (enable/disable): STRATEGY_REBALANCING, STRATEGY_COMBINATORIAL, "
                    "STRATEGY_LATENCY_ARB, STRATEGY_MARKET_MAKING, STRATEGY_RESOLUTION, "
                    "STRATEGY_EVENT_DRIVEN\n\n"

                    "DECISION RULES (override your own judgment only with strong evidence):\n"
                    "  - bootstrap_phase=true: DO NOT change any parameters. Observe only.\n"
                    "  - Health CRITICAL AND NOT bootstrap: disable worst ROI strategy, raise MIN_EDGE_THRESHOLD 20%\n"
                    "  - Win rate < 40% AND >= 30 closed positions: raise MIN_EDGE_THRESHOLD\n"
                    "  - Strategy ROI < -1% AND >= 30 trades: disable that strategy\n"
                    "  - Fee drag > 1.5% of volume AND >= 30 trades: raise MIN_EDGE_THRESHOLD\n"
                    "  - trades_per_hour > 20: raise MIN_TRADE_INTERVAL\n"
                    "  - trades_per_hour < 0.5 AND health HEALTHY: lower MIN_EDGE_THRESHOLD 10%\n"
                    "  - Best strategy ROI > 2% AND >= 30 trades: consider raising MAX_POSITION_SIZE\n\n"

                    "OUTPUT FORMAT:\n"
                    "Write 3 sections:\n"
                    "## Performance Summary\n"
                    "One paragraph: health grade, top performer, worst performer, key concern.\n\n"
                    "## Analysis\n"
                    "Per-strategy assessment (1 line each) using ROI and win rate data.\n\n"
                    "## Decision\n"
                    "What you are changing and why (cite the specific data point). "
                    "Then provide the JSON block with ONLY changed keys:\n"
                    "```json\n{...}\n```\n"
                    "If no changes needed, return ```json\n{}\n```"
                )

                user_msg = (
                    f"Analyze this Polymarket arb bot cycle.\n\n"
                    f"**Live Risk Health:**\n```json\n{json.dumps(live_health, indent=2)}\n```\n\n"
                    f"**Current Config:**\n```json\n{json.dumps(current_env, indent=2)}\n```\n\n"
                    f"**Performance Snapshot:**\n```json\n{json.dumps(analysis_data, indent=2)}\n```\n\n"
                    "Identify the weakest strategy by ROI, check if health requires defensive action, "
                    "and suggest only evidence-backed parameter changes."
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

                # Auto-apply proposed changes to live config
                # Skip auto-apply during bootstrap phase (< 30 closed positions)
                bootstrap = analysis_data.get("health", {}).get("bootstrap_phase", True)
                applied = []
                if proposed and not bootstrap:
                    applied = self._apply_config_overrides(proposed)
                    if applied:
                        # Persist to disk so changes survive restarts
                        config_path = "logs/meta_agent_config.json"
                        try:
                            existing = {}
                            if os.path.exists(config_path):
                                with open(config_path) as f:
                                    existing = json.load(f)
                            existing.update(proposed)
                            with open(config_path, "w") as f:
                                json.dump(existing, f, indent=2)
                        except Exception as exc:
                            logger.warning(f"Meta-agent: could not persist config: {exc}")
                        logger.info(f"Meta-agent: AUTO-APPLIED {len(applied)} change(s): {applied}")
                    else:
                        logger.info("Meta-agent: no config changes needed")
                elif proposed and bootstrap:
                    logger.info(
                        f"Meta-agent: bootstrap phase ({analysis_data['health'].get('closed_positions', 0)} "
                        f"closed positions) — logging suggestions but NOT auto-applying changes"
                    )

                log_entry = {
                    "timestamp": time.time(),
                    "analysis": text,
                    "proposed_changes": proposed,
                    "applied_changes": applied,
                    "current_values": current_env,
                    # Store only the health summary, not full snapshot (saves disk)
                    "health": analysis_data.get("health", {}),
                    "strategy_roi_pct": analysis_data.get("strategy_roi_pct", {}),
                    "portfolio_summary": analysis_data.get("portfolio", {}),
                }
                os.makedirs("logs", exist_ok=True)
                log_path = f"logs/meta_agent_{int(time.time())}.json"
                with open(log_path, "w") as f:
                    json.dump(log_entry, f, separators=(",", ":"))

                # Prune old meta-agent logs — keep only the last 48 (24h at 30-min intervals)
                self._prune_meta_agent_logs(keep=48)

                logger.info(f"Meta-agent: analysis complete — {len(proposed)} suggestion(s), {len(applied)} applied")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Meta-agent error: {exc}")

    # ------------------------------------------------------------------ #
    #  Live config tuning                                                  #
    # ------------------------------------------------------------------ #

    _CONFIG_MAP: dict[str, tuple[str, str, type]] = {
        # env_key: (config_section, attribute, type)
        "MIN_EDGE_THRESHOLD":      ("risk",       "min_edge_threshold",      float),
        "MAX_POSITION_SIZE":       ("risk",       "max_position_size",        float),
        "MAX_TOTAL_EXPOSURE":      ("risk",       "max_total_exposure",       float),
        "MAX_SLIPPAGE":            ("risk",       "max_slippage",             float),
        "MIN_TRADE_INTERVAL":      ("risk",       "min_trade_interval",       int),
        "TOKEN_COOLDOWN":          ("risk",       "token_cooldown",           int),
        "MM_SPREAD_BPS":           ("strategies", "mm_spread_bps",            int),
        "MM_ORDER_SIZE":           ("strategies", "mm_order_size",            float),
        "MM_MAX_INVENTORY":        ("strategies", "mm_max_inventory",         float),
        "MAX_DAYS_TO_RESOLUTION":  ("strategies", "max_days_to_resolution",   int),
        "MAX_MARKETS":             ("strategies", "max_markets",              int),
        "STRATEGY_REBALANCING":    ("strategies", "rebalancing_enabled",      bool),
        "STRATEGY_COMBINATORIAL":  ("strategies", "combinatorial_enabled",    bool),
        "STRATEGY_MARKET_MAKING":  ("strategies", "market_making_enabled",    bool),
        "STRATEGY_RESOLUTION":     ("strategies", "resolution_enabled",       bool),
        "STRATEGY_EVENT_DRIVEN":   ("strategies", "event_driven_enabled",     bool),
        "STRATEGY_QUICK_RESOLUTION": ("strategies", "quick_resolution_enabled", bool),
        "STRATEGY_ENSEMBLE":         ("strategies", "ensemble_enabled",          bool),
    }

    def _apply_config_overrides(self, overrides: dict) -> list[str]:
        """Apply a dict of {ENV_KEY: value} to the live config. Returns list of applied keys."""
        applied = []
        strategy_flags_changed = False

        for key, val in overrides.items():
            mapping = self._CONFIG_MAP.get(key)
            if not mapping:
                continue
            section, attr, typ = mapping
            cfg_obj = self.config.risk if section == "risk" else self.config.strategies
            try:
                if typ == bool:
                    coerced = str(val).lower() in ("true", "1", "yes")
                else:
                    coerced = typ(val)
                old = getattr(cfg_obj, attr)
                if old != coerced:
                    setattr(cfg_obj, attr, coerced)
                    logger.info(f"[Config] {key}: {old} → {coerced}")
                    applied.append(key)
                    if section == "strategies" and attr.endswith("_enabled"):
                        strategy_flags_changed = True
            except (ValueError, TypeError) as exc:
                logger.warning(f"[Config] Could not apply {key}={val}: {exc}")

        if strategy_flags_changed:
            self._build_strategies()
            logger.info(f"[Config] Strategy list rebuilt: {[type(s).__name__ for s in self._strategies]}")

        return applied

    def _load_saved_config(self) -> None:
        """Load and apply any previously auto-tuned config from disk."""
        import json
        path = "logs/meta_agent_config.json"
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                saved = json.load(f)
            applied = self._apply_config_overrides(saved)
            if applied:
                logger.info(f"[Config] Restored {len(applied)} auto-tuned parameter(s) from disk: {applied}")
        except Exception as exc:
            logger.warning(f"[Config] Could not load saved config: {exc}")

    def _process_pending_deploys(self) -> None:
        """Hot-load any strategy files deployed via the dashboard."""
        import importlib.util as _ilu
        import src.dashboard.app as _dash_mod
        from src.strategies.base import BaseStrategy

        pending = _dash_mod.get_pending_deploys()
        if not pending:
            return
        _dash_mod.clear_pending_deploys()

        for deploy in pending:
            file_path = deploy.get("file_path", "")
            class_name = deploy.get("class_name", "")
            if not file_path or not os.path.exists(file_path):
                logger.warning(f"Deploy: file not found: {file_path}")
                continue
            try:
                spec = _ilu.spec_from_file_location(f"dynamic_{class_name}", file_path)
                mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Find the Strategy subclass
                klass = None
                for name, obj in mod.__dict__.items():
                    if (isinstance(obj, type)
                            and issubclass(obj, BaseStrategy)
                            and obj is not BaseStrategy
                            and name == class_name):
                        klass = obj
                        break
                if not klass:
                    logger.warning(f"Deploy: class {class_name} not found in {file_path}")
                    continue
                instance = klass(self.config, self.portfolio, self.risk)
                self._dynamic_strategies.append(instance)
                self._strategies.append(instance)
                logger.info(f"[Deploy] Hot-loaded strategy: {class_name} from {file_path}")
            except Exception as exc:
                logger.error(f"Deploy: failed to load {file_path}: {exc}")

    def _prune_meta_agent_logs(self, keep: int = 48) -> None:
        """Delete oldest meta_agent_*.json files, keeping only the most recent `keep`."""
        import glob as _glob
        files = sorted(_glob.glob("logs/meta_agent_[0-9]*.json"))
        for old in files[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass

    async def _research_loop(self) -> None:
        """Hourly web research for new strategies and improvements."""
        import glob as _glob
        from src.meta_agent.researcher import run_research, DEFAULT_INTERVAL_HOURS

        if not os.getenv("ANTHROPIC_API_KEY"):
            return

        interval_hours = float(os.getenv("RESEARCH_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS))
        interval_secs = interval_hours * 3600

        # If a recent run exists, wait for its interval to expire
        existing = sorted(_glob.glob("logs/research_*.json"), reverse=True)
        if existing:
            try:
                age_secs = time.time() - os.path.getmtime(existing[0])
                wait = max(0.0, interval_secs - age_secs)
                if wait > 0:
                    logger.info(f"Research agent: last run {age_secs/3600:.1f}h ago, next in {wait/3600:.1f}h")
                    await asyncio.sleep(wait)
            except OSError:
                pass
        else:
            await asyncio.sleep(60)  # brief warm-up on very first run

        while self._running:
            try:
                await run_research()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Research agent error: {exc}")

            await asyncio.sleep(interval_secs)

    async def _code_review_loop(self) -> None:
        """Run a weekly read-only code review via Claude. Never auto-modifies code."""
        import glob as _glob
        from src.meta_agent.code_reviewer import run_code_review

        if not os.getenv("ANTHROPIC_API_KEY"):
            return

        # Wait for any recent review to expire before running
        existing = sorted(_glob.glob("logs/code_review_*.json"), reverse=True)
        if existing:
            try:
                age_days = (time.time() - os.path.getmtime(existing[0])) / 86400
                wait_secs = max(0.0, (7.0 - age_days) * 86400)
                if wait_secs > 0:
                    logger.info(
                        f"Code review: last review {age_days:.1f}d ago, "
                        f"next in {wait_secs/3600:.1f}h"
                    )
                    await asyncio.sleep(wait_secs)
            except OSError:
                pass
        else:
            # First run — let the bot warm up for 5 minutes first
            await asyncio.sleep(300)

        while self._running:
            try:
                await run_code_review()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Code review error: {exc}")

            await asyncio.sleep(7 * 24 * 3600)  # run weekly

    def _startup_cleanup(self) -> None:
        """
        Run once at startup to clear accumulated log bloat from previous deploys.
        Removes old date-stamped log files and excess meta-agent logs.
        Keeps portfolio_state.json and meta_agent_config.json intact.
        """
        import glob as _glob
        removed = 0

        # Delete old date-stamped log files (bot_YYYY-MM-DD.log) — replaced by bot.log
        for f in _glob.glob("logs/bot_20*.log") + _glob.glob("logs/bot_20*.log.gz"):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass

        # Delete oversized current log file if it somehow got too big
        try:
            if os.path.getsize("logs/bot.log") > 50 * 1024 * 1024:  # >50MB
                os.remove("logs/bot.log")
                removed += 1
        except OSError:
            pass

        # Prune meta-agent logs to last 48
        self._prune_meta_agent_logs(keep=48)

        if removed:
            logger.info(f"[Startup] Cleaned up {removed} old log file(s)")

    def _handle_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self._running = False

    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")
        await self.binance.stop()
        if self._ws_feed:
            await self._ws_feed.stop()
        if self._news_monitor:
            try:
                await self._news_monitor.stop()
            except Exception:
                pass
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
