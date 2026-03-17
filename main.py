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
        self.portfolio.load_from_json()  # restore state from previous run if available
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
        # Rate limiting
        self._last_trade_time: float = 0.0
        self._token_last_traded: dict[str, float] = {}

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
        dashboard_register(self.portfolio, self._start_time,
                           config=self.config, risk=self.risk,
                           binance=self.binance, kalshi=self._kalshi)

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

            # Clean up old log bloat from previous deploys
            self._startup_cleanup()

            # Apply any previously auto-tuned config
            self._load_saved_config()

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

        now = time.time()
        min_interval = self.config.risk.min_trade_interval
        token_cooldown = self.config.risk.token_cooldown

        for sig in signals:
            key = (sig.token_id, sig.side)
            if key in seen:
                continue
            seen.add(key)

            if self.risk.is_hard_stopped():
                logger.critical("Hard stop active — no new trades")
                break

            # Global rate limit: enforce minimum gap between any two trades
            since_last = now - self._last_trade_time
            if since_last < min_interval:
                logger.debug(f"Rate limit: {since_last:.0f}s since last trade (need {min_interval}s)")
                break  # skip rest of signals this cycle

            # Per-token cooldown: don't hammer the same market
            token_last = self._token_last_traded.get(sig.token_id, 0)
            if now - token_last < token_cooldown:
                logger.debug(f"Token cooldown: {sig.token_id[:16]} traded {now-token_last:.0f}s ago")
                continue

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
                self._last_trade_time = time.time()
                self._token_last_traded[sig.token_id] = time.time()

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
        min_trades = 20   # need meaningful sample before analyzing

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
                if len(snapshot.trades) < min_trades:
                    logger.info(f"Meta-agent: only {len(snapshot.trades)} trades so far (need {min_trades}), skipping")
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
                elif proposed and bootstrap:
                    logger.info(
                        f"Meta-agent: bootstrap phase ({analysis_data['health'].get('closed_positions', 0)} "
                        f"closed positions) — logging suggestions but NOT auto-applying changes"
                    )
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
        "STRATEGY_LATENCY_ARB":    ("strategies", "latency_arb_enabled",      bool),
        "STRATEGY_MARKET_MAKING":  ("strategies", "market_making_enabled",    bool),
        "STRATEGY_RESOLUTION":     ("strategies", "resolution_enabled",       bool),
        "STRATEGY_EVENT_DRIVEN":   ("strategies", "event_driven_enabled",     bool),
    }

    def _apply_config_overrides(self, overrides: dict) -> list[str]:
        """Apply a dict of {ENV_KEY: value} to the live config. Returns list of applied keys."""
        import json as _json
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
        import json as _json
        path = "logs/meta_agent_config.json"
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                saved = _json.load(f)
            applied = self._apply_config_overrides(saved)
            if applied:
                logger.info(f"[Config] Restored {len(applied)} auto-tuned parameter(s) from disk: {applied}")
        except Exception as exc:
            logger.warning(f"[Config] Could not load saved config: {exc}")

    def _prune_meta_agent_logs(self, keep: int = 48) -> None:
        """Delete oldest meta_agent_*.json files, keeping only the most recent `keep`."""
        import glob as _glob
        files = sorted(_glob.glob("logs/meta_agent_[0-9]*.json"))
        for old in files[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass

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
