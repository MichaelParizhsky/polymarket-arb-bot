"""
Strategy: Optimism Tax (Maker-Side Longshot Edge)

Based on Jon Becker's microstructure research: takers systematically overpay
for YES contracts at longshot prices (1–20¢). At 1¢ YES, actual win rate is
0.43% vs implied 1% — a 57% mispricing. We exploit this by:

1. Identifying YES longshots (1–20¢) across eligible categories
2. Computing Bayesian posterior probability of NO winning
3. Running Monte Carlo simulation to verify positive EV
4. Sizing with fractional Kelly (0.25x)
5. Posting LIMIT ORDERS on NO side (we are makers, not takers)

Win rate target: 94–99% (longshot NO positions almost always win)
Edge source: systematic optimism bias of YES takers
"""
from __future__ import annotations

import time
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.filters.category_filter import classify_market, get_category_edge, is_tradeable_category
from src.models.bayesian import BayesianEngine
from src.models.kelly import KellySizer
from src.models.monte_carlo import MonteCarloResult, should_trade, simulate_trade
from src.strategies.base import BaseStrategy, Signal
from src.utils.constants import MIN_TRADE_USDC
from src.utils.metrics import arb_opportunities, edge_detected


class OptimismTaxStrategy(BaseStrategy):
    """
    Maker-side longshot edge: buy NO on markets where YES trades at 1–20¢.

    Takers systematically overpay for YES longshots because of optimism bias.
    At 1¢ YES, empirical win rate is ~0.43% vs the implied 1% — a 57%
    overpricing. We post LIMIT orders on NO and collect the edge as makers.

    The strategy is filtered by:
    - Category edge (Becker research): crypto/weather/sports preferred
    - Bayesian calibration: posterior NO probability vs market-implied price
    - Monte Carlo P(profit) >= min_p_profit (default 90%)
    - Fractional Kelly sizing (0.25×) with configurable max spend
    - Per-market cooldown to avoid duplicate entries

    Signals reference the NO token with side="BUY" at a limit price derived
    from the YES best_ask (no_price = 1 - yes_best_ask).
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        # condition_id -> timestamp of last entry
        self._entered: dict[str, float] = {}
        self._bayesian = BayesianEngine()
        cfg = config.strategies
        kelly_fraction = 0.25
        max_bet = getattr(cfg, "optimism_tax_max_spend", 150.0)
        self._kelly = KellySizer(fraction=kelly_fraction, max_bet=max_bet)

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        signals: list[Signal] = []

        cfg = self.config.strategies
        enabled: bool = getattr(cfg, "optimism_tax_enabled", True)
        if not enabled:
            return signals

        min_yes_price: float = getattr(cfg, "optimism_tax_min_yes_price", 0.01)
        max_yes_price: float = getattr(cfg, "optimism_tax_max_yes_price", 0.20)
        min_edge: float = getattr(cfg, "optimism_tax_min_edge", 0.005)
        max_spend: float = getattr(cfg, "optimism_tax_max_spend", 150.0)
        min_volume: float = getattr(cfg, "optimism_tax_min_volume", 5000.0)
        cooldown_hours: float = getattr(cfg, "optimism_tax_cooldown_hours", 6.0)
        min_p_profit: float = getattr(cfg, "optimism_tax_min_p_profit", 0.90)

        # Prune expired cooldowns
        cutoff = time.time() - cooldown_hours * 3600.0
        expired_cids = [cid for cid, ts in self._entered.items() if ts < cutoff]
        for cid in expired_cids:
            del self._entered[cid]

        skipped_inactive = 0
        skipped_volume = 0
        skipped_cooldown = 0
        skipped_no_longshot = 0
        skipped_category = 0
        skipped_bayesian = 0
        skipped_mc = 0
        skipped_risk = 0

        for market in markets:
            if not market.active or market.closed:
                skipped_inactive += 1
                continue

            volume = market.volume or 0.0
            if volume < min_volume:
                skipped_volume += 1
                continue

            if market.condition_id in self._entered:
                skipped_cooldown += 1
                continue

            # Locate YES and NO tokens
            yes_token = next(
                (t for t in market.tokens if t.outcome.lower() == "yes"),
                market.tokens[0] if market.tokens else None,
            )
            no_token = next(
                (t for t in market.tokens if t.outcome.lower() == "no"),
                market.tokens[1] if len(market.tokens) > 1 else None,
            )
            if yes_token is None or no_token is None:
                continue

            yes_book = orderbooks.get(yes_token.token_id)
            no_book = orderbooks.get(no_token.token_id)
            if yes_book is None:
                continue

            yes_ask = yes_book.best_ask
            if yes_ask is None:
                continue

            # Core filter: YES must be a longshot
            if not (min_yes_price <= yes_ask <= max_yes_price):
                skipped_no_longshot += 1
                continue

            # Category filter — avoid near-efficient categories (finance)
            category = classify_market(market.question, market.tags or [])
            category_edge = get_category_edge(category)
            if not is_tradeable_category(category, min_edge=min_edge):
                skipped_category += 1
                self.log(
                    f"skip [{category}] edge={category_edge:.4f} < {min_edge:.4f} | "
                    f"{market.question[:60]}",
                    level="debug",
                )
                continue

            # NO entry price: since YES + NO = $1.00 at settlement,
            # if YES trades at X¢ the NO is worth (1 - X)¢.
            # We post a limit at 1 - yes_ask so we are makers on the NO side.
            no_entry_price: float = round(1.0 - yes_ask, 4)
            if no_entry_price <= 0.0 or no_entry_price >= 1.0:
                continue

            # Bayesian calibration: get posterior NO probability
            try:
                calibrated_yes_prob = self._bayesian.get_calibrated_prob(yes_ask, category)
                alpha, beta_param = self._bayesian.get_posterior_beta(yes_ask, category)
            except Exception as exc:
                self.log(f"Bayesian error for {market.question[:50]}: {exc}", level="warning")
                skipped_bayesian += 1
                continue

            true_no_prob = 1.0 - calibrated_yes_prob
            gross_edge = true_no_prob - no_entry_price

            # Maker orders apply a conservative 0.1% cost + half the category edge bonus
            net_edge = gross_edge - 0.001 + (category_edge * 0.5)

            if net_edge < min_edge:
                self.log(
                    f"skip low edge={net_edge:.4f} | yes_ask={yes_ask:.3f} "
                    f"true_no_prob={true_no_prob:.4f} | {market.question[:60]}",
                    level="debug",
                )
                skipped_bayesian += 1
                continue

            # Kelly sizing
            kelly_size = self._kelly.compute(
                win_prob=true_no_prob,
                entry_price=no_entry_price,
                bankroll=self.portfolio.usdc_balance,
            )
            kelly_size = min(kelly_size, max_spend)

            if kelly_size < MIN_TRADE_USDC:
                skipped_risk += 1
                continue

            # Monte Carlo validation
            try:
                mc_result: MonteCarloResult = simulate_trade(
                    alpha=alpha,
                    beta=beta_param,
                    entry_price=no_entry_price,
                    size_usdc=kelly_size,
                )
            except Exception as exc:
                self.log(f"MC error for {market.question[:50]}: {exc}", level="warning")
                skipped_mc += 1
                continue

            if not should_trade(mc_result, min_p_profit=min_p_profit):
                self.log(
                    f"MC reject: p_profit={mc_result.p_profit:.3f} < {min_p_profit:.3f} | "
                    f"yes={yes_ask:.3f} no_entry={no_entry_price:.3f} | {market.question[:60]}",
                    level="debug",
                )
                skipped_mc += 1
                continue

            # Risk manager check
            ok, reason = self.risk.check_trade(no_token.token_id, "BUY", kelly_size, "optimism_tax")
            if not ok:
                self.log(f"risk blocked: {reason}", level="debug")
                skipped_risk += 1
                continue

            size_usdc = min(kelly_size, max_spend)
            if size_usdc < MIN_TRADE_USDC:
                skipped_risk += 1
                continue

            # Record entry to enforce cooldown
            self._entered[market.condition_id] = time.time()

            # Prometheus metrics
            arb_opportunities.labels(strategy="optimism_tax").inc()
            edge_detected.labels(strategy="optimism_tax").observe(net_edge)

            self.log(
                f"[OPTIMISM TAX] BUY NO @ {no_entry_price:.4f} (limit) | "
                f"yes_ask={yes_ask:.3f} | cat={category} | "
                f"true_no_prob={true_no_prob:.4f} | cat_edge={category_edge:.4f} | "
                f"net_edge={net_edge:.4f} | kelly=${kelly_size:.2f} | size=${size_usdc:.2f} | "
                f"MC p_profit={mc_result.p_profit:.3f} median_ev={mc_result.median_ev:.4f} | "
                f"{market.question[:70]}"
            )

            signals.append(
                Signal(
                    strategy="optimism_tax",
                    token_id=no_token.token_id,
                    side="BUY",
                    price=no_entry_price,
                    size_usdc=size_usdc,
                    edge=net_edge,
                    notes=(
                        f"[OPTIMISM_TAX] NO limit @ {no_entry_price:.4f} | "
                        f"yes_ask={yes_ask:.3f} | cat={category} | "
                        f"p_no={true_no_prob:.4f} | edge={net_edge:.4f} | "
                        f"MC_p={mc_result.p_profit:.3f}"
                    ),
                    metadata={
                        "yes_ask": yes_ask,
                        "no_entry_price": no_entry_price,
                        "category": category,
                        "category_edge": category_edge,
                        "calibrated_yes_prob": calibrated_yes_prob,
                        "true_no_prob": true_no_prob,
                        "gross_edge": gross_edge,
                        "net_edge": net_edge,
                        "kelly_size": kelly_size,
                        "mc_p_profit": mc_result.p_profit,
                        "mc_median_ev": mc_result.median_ev,
                        "mc_mean_ev": mc_result.mean_ev,
                        "mc_p5": mc_result.p5,
                        "bayesian_alpha": alpha,
                        "bayesian_beta": beta_param,
                        "condition_id": market.condition_id,
                        "market_question": market.question,
                        "entered_at": time.time(),
                    },
                )
            )

        self.log(
            f"scan: {len(markets)} total, {len(signals)} signals | "
            f"skipped: {skipped_inactive} inactive, {skipped_volume} volume, "
            f"{skipped_cooldown} cooldown, {skipped_no_longshot} not-longshot, "
            f"{skipped_category} category, {skipped_bayesian} bayesian/edge, "
            f"{skipped_mc} MC, {skipped_risk} risk"
        )

        return signals
