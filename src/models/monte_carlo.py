"""
Monte Carlo pre-trade simulator.

Runs N=1000 simulations sampling from the posterior Beta distribution
to compute median EV, P(profit), and percentile bounds before entering
any trade. Only proceed if median EV > 0 and P(profit) > min_p_profit.

Fee model: 0.2% taker fee on proceeds (Polymarket standard maker fill).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class MonteCarloResult:
    """Results from a single pre-trade Monte Carlo simulation."""
    median_ev: float       # Median EV across N paths (USDC)
    mean_ev: float         # Mean EV
    p_profit: float        # Fraction of paths with positive EV
    p5: float              # 5th-percentile EV (downside risk)
    p95: float             # 95th-percentile EV (upside potential)
    std_ev: float          # Standard deviation of EV distribution
    n_simulations: int     # Number of paths run


def simulate_trade(
    alpha: float,
    beta: float,
    entry_price: float,
    size_usdc: float,
    n: int = 1000,
    fee_rate: float = 0.002,
    rng: np.random.Generator | None = None,
) -> MonteCarloResult:
    """Run a Monte Carlo simulation for a prospective NO-side limit order.

    Samples win probability from Beta(alpha, beta) to represent uncertainty
    in the calibrated posterior. For each sample:
    - With probability (1 - p_yes) [NO wins]: receive size_usdc / entry_price * (1 - fee_rate)
    - With probability p_yes [YES wins]:     lose size_usdc entirely

    win_probs are sampled YES win probabilities from Beta(alpha, beta).
    NO wins when the uniform draw >= win_prob (i.e. YES event does not occur).

    Parameters
    ----------
    alpha, beta:
        Beta distribution shape parameters (from BayesianEngine.get_posterior_beta).
    entry_price:
        The NO token limit order price in (0, 1).
    size_usdc:
        Capital to risk in USDC.
    n:
        Number of simulation paths (default 1000).
    fee_rate:
        Polymarket taker fee rate applied on proceeds (default 0.002 = 0.2%).

    Returns
    -------
    MonteCarloResult
        Distribution statistics across all simulated paths.
    """
    if alpha <= 0 or beta <= 0:
        raise ValueError(f"Alpha and beta must be > 0, got alpha={alpha}, beta={beta}")
    if not (0.0 < entry_price < 1.0):
        raise ValueError(f"entry_price must be in (0, 1), got {entry_price}")
    if size_usdc <= 0:
        raise ValueError(f"size_usdc must be > 0, got {size_usdc}")

    if rng is None:
        rng = np.random.default_rng()

    # Sample N win probabilities from the posterior Beta distribution.
    win_probs = rng.beta(alpha, beta, size=n)

    # For each path, simulate a single binary outcome.
    outcomes = rng.random(size=n)  # uniform [0, 1]
    wins = outcomes >= win_probs   # True where NO wins (YES event does NOT occur)

    # Compute P&L per path.
    # Win:  receive contracts * $1 at resolution, minus cost and fee
    #       contracts = size_usdc / entry_price
    #       gross_proceeds = contracts * 1.0 = size_usdc / entry_price
    #       net_proceeds = gross_proceeds * (1 - fee_rate)
    #       pnl = net_proceeds - size_usdc
    contracts = size_usdc / entry_price
    gross_win = contracts * (1.0 - fee_rate)  # net proceeds when NO resolves to $1
    pnl_win = gross_win - size_usdc

    # Loss: forfeit entire stake
    pnl_loss = -size_usdc

    ev_per_path = np.where(wins, pnl_win, pnl_loss)

    return MonteCarloResult(
        median_ev=float(np.median(ev_per_path)),
        mean_ev=float(np.mean(ev_per_path)),
        p_profit=float(np.mean(ev_per_path > 0)),
        p5=float(np.percentile(ev_per_path, 5)),
        p95=float(np.percentile(ev_per_path, 95)),
        std_ev=float(np.std(ev_per_path)),
        n_simulations=n,
    )


def should_trade(
    result: MonteCarloResult,
    min_ev: float = 0.0,
    min_p_profit: float = 0.55,
) -> bool:
    """Return True if the Monte Carlo result clears the entry thresholds.

    Parameters
    ----------
    result:
        Output from :func:`simulate_trade`.
    min_ev:
        Minimum acceptable median EV in USDC (default 0 = any positive).
    min_p_profit:
        Minimum fraction of profitable paths (default 0.55 = 55%).
        For NO-side longshot trades targeting 90%+ win rate, set to 0.90.

    Returns
    -------
    bool
        True if both conditions are satisfied.
    """
    return result.median_ev > min_ev and result.p_profit >= min_p_profit
