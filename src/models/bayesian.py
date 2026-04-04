"""
Bayesian probability engine.

Uses Becker's calibration data to compute posterior win probabilities.
At 1¢ YES, actual win rate is 0.43% vs implied 1% (57% mispricing).
The posterior is a Beta distribution representing uncertainty about the
true probability given the observed market price and category context.

The key insight from the microstructure research is that retail takers
systematically overprice longshot YES contracts — they "buy hope" and
create a persistent edge for NO-side makers.
"""

from __future__ import annotations

import math
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Becker calibration table: market-implied probability → actual win rate.
# Keys are implied YES prices (floats).  Values are actual observed win rates.
# Interpolation is used for prices that fall between buckets.
# ---------------------------------------------------------------------------
PRICE_BUCKET_CALIBRATION: dict[float, float] = {
    0.01: 0.0043,   # 1% implied  → 0.43% actual  (calibration factor 0.43)
    0.02: 0.0110,   # 2% implied  → 1.10% actual
    0.05: 0.0310,   # 5% implied  → 3.10% actual  (calibration factor 0.62)
    0.10: 0.0750,   # 10% implied → 7.50% actual  (calibration factor 0.75)
    0.15: 0.1220,   # 15% implied → 12.20% actual
    0.20: 0.1640,   # 20% implied → 16.40% actual (calibration factor 0.82)
    0.30: 0.2680,   # 30% implied → 26.80% actual (calibration factor 0.893)
    0.50: 0.5000,   # 50% implied → 50.00% actual (efficient — no bias)
    0.70: 0.7200,   # 70% implied → 72.00% actual (slight NO underpricing)
    0.80: 0.8300,   # 80% implied → 83.00% actual
    0.90: 0.9250,   # 90% implied → 92.50% actual
}

# Category-specific adjustment factors applied on top of Becker calibration.
# Positive values increase the calibrated YES probability (reducing NO edge);
# negative values widen the NO edge further.
# Derived from category-level maker edge differentials in the research.
_CATEGORY_YES_ADJUSTMENT: dict[str, float] = {
    "crypto":        -0.005,   # extra pessimism due to high volatility framing
    "weather":       -0.003,
    "sports":        -0.002,
    "politics":      +0.001,   # markets more informed — tighter edge
    "finance":       +0.004,   # near-efficient, slight YES overpricing
    "entertainment": -0.008,   # large retail frenzy → more mispricing
    "world":         -0.012,   # highest edge category
    "unknown":        0.000,
}

# Sorted calibration keys for interpolation lookup (ascending order).
_SORTED_BUCKETS: list[float] = sorted(PRICE_BUCKET_CALIBRATION.keys())


class _BayesianResult(NamedTuple):
    calibrated_yes_prob: float
    no_edge: float
    alpha: float
    beta: float


class BayesianEngine:
    """Compute posterior probabilities for YES/NO on Polymarket binary markets.

    All methods are pure functions of their inputs (no mutable state), so a
    single shared instance is safe to use across coroutines.

    Parameters
    ----------
    calibration_table:
        Override the default Becker calibration table.  Must be a dict of
        {implied_yes_price: actual_win_rate} with at least two entries.
    """

    def __init__(
        self,
        calibration_table: dict[float, float] | None = None,
    ) -> None:
        if calibration_table is not None:
            self._table = calibration_table
            self._sorted_keys = sorted(calibration_table.keys())
        else:
            self._table = PRICE_BUCKET_CALIBRATION
            self._sorted_keys = _SORTED_BUCKETS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_calibrated_prob(
        self,
        market_price: float,
        category: str = "unknown",
    ) -> float:
        """Return the calibrated YES probability given a market-implied price.

        Applies:
        1. Becker bucket interpolation to correct optimism bias.
        2. Category-specific adjustment.
        3. Hard clamp to [0.001, 0.999] to keep Beta params valid.

        Parameters
        ----------
        market_price:
            Current best-ask (or mid) for the YES token, in [0, 1].
        category:
            Market category string from the category filter.

        Returns
        -------
        float
            Posterior estimate of the true YES win probability.
        """
        if not (0.0 < market_price < 1.0):
            raise ValueError(
                f"market_price must be in (0, 1), got {market_price!r}"
            )

        calibrated = self.interpolate_calibration(market_price)
        adjustment = _CATEGORY_YES_ADJUSTMENT.get(category, 0.0)
        adjusted = calibrated + adjustment
        return max(0.001, min(0.999, adjusted))

    def get_posterior_beta(
        self,
        market_price: float,
        category: str = "unknown",
        n_obs: int = 100,
    ) -> tuple[float, float]:
        """Return Beta distribution parameters (alpha, beta) for the posterior.

        The posterior is formed by treating the calibrated probability as the
        mean of a Beta distribution and scaling the pseudo-count (n_obs) to
        represent our confidence in the calibration.

        Method of moments:
            alpha = mu * n_obs
            beta  = (1 - mu) * n_obs

        Parameters
        ----------
        market_price:
            Current YES token price in (0, 1).
        category:
            Market category string.
        n_obs:
            Pseudo-observation count controlling posterior concentration.
            Higher values → tighter distribution → less Monte Carlo variance.
            Default 100 reflects moderate confidence in the calibration data.

        Returns
        -------
        tuple[float, float]
            (alpha, beta) parameters for scipy.stats.beta or numpy sampling.
        """
        if n_obs < 2:
            raise ValueError(f"n_obs must be >= 2, got {n_obs!r}")

        mu = self.get_calibrated_prob(market_price, category)
        alpha = mu * n_obs
        beta_param = (1.0 - mu) * n_obs
        return alpha, beta_param

    def get_no_edge(
        self,
        yes_price: float,
        category: str = "unknown",
    ) -> float:
        """Compute the net edge from buying NO at (1 - yes_price).

        Edge = implied NO win probability − market NO price.

        A positive value means NO is underpriced: the market assigns a higher
        YES probability than the data-calibrated estimate supports.

        Parameters
        ----------
        yes_price:
            Current YES token price in (0, 1).
        category:
            Market category string.

        Returns
        -------
        float
            Edge in probability-point units.  Positive → profitable NO trade.
        """
        calibrated_yes = self.get_calibrated_prob(yes_price, category)
        calibrated_no = 1.0 - calibrated_yes
        market_no_price = 1.0 - yes_price
        return calibrated_no - market_no_price

    def interpolate_calibration(self, yes_price: float) -> float:
        """Linearly interpolate the calibration table for a given YES price.

        Clamps to the table's edge values for out-of-range inputs.

        Parameters
        ----------
        yes_price:
            Implied YES probability from the market in (0, 1).

        Returns
        -------
        float
            Calibrated (true) YES win probability estimate.
        """
        keys = self._sorted_keys
        table = self._table

        # Below lowest bucket — clamp to lowest actual rate.
        if yes_price <= keys[0]:
            return table[keys[0]]

        # Above highest bucket — clamp to highest actual rate.
        if yes_price >= keys[-1]:
            return table[keys[-1]]

        # Find surrounding buckets via binary search.
        lo_idx = 0
        hi_idx = len(keys) - 1
        while lo_idx + 1 < hi_idx:
            mid = (lo_idx + hi_idx) // 2
            if keys[mid] <= yes_price:
                lo_idx = mid
            else:
                hi_idx = mid

        lo_price = keys[lo_idx]
        hi_price = keys[hi_idx]
        lo_actual = table[lo_price]
        hi_actual = table[hi_price]

        # Linear interpolation weight.
        t = (yes_price - lo_price) / (hi_price - lo_price)
        return lo_actual + t * (hi_actual - lo_actual)

    def analyze(
        self,
        yes_price: float,
        category: str = "unknown",
        n_obs: int = 100,
    ) -> _BayesianResult:
        """Convenience method returning all Bayesian metrics in one call.

        Parameters
        ----------
        yes_price:
            Current YES token market price in (0, 1).
        category:
            Market category string.
        n_obs:
            Pseudo-observation count for Beta posterior (see :meth:`get_posterior_beta`).

        Returns
        -------
        _BayesianResult
            Named tuple with calibrated_yes_prob, no_edge, alpha, beta.
        """
        calibrated_yes = self.get_calibrated_prob(yes_price, category)
        no_edge = self.get_no_edge(yes_price, category)
        alpha, beta_param = self.get_posterior_beta(yes_price, category, n_obs)
        return _BayesianResult(
            calibrated_yes_prob=calibrated_yes,
            no_edge=no_edge,
            alpha=alpha,
            beta=beta_param,
        )
