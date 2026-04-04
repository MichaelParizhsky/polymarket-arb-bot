"""
Kelly criterion position sizer.

Uses fractional Kelly (f* = 0.25) to avoid overbetting and ruin.
Accounts for Polymarket's maker fee in the net-odds calculation.

Reference formula:
    f* = (b * p - q) / b
    where:
        b   = net odds (profit per $1 wagered if we win)
        p   = posterior win probability
        q   = 1 - p (loss probability)
        f*  = optimal fraction of bankroll to wager

We apply a fraction multiplier (default 0.25) for safety and enforce
hard dollar caps to prevent catastrophic single-trade losses.
"""
from __future__ import annotations


class KellySizer:
    """Compute fractional Kelly position sizes for binary prediction markets.

    Parameters
    ----------
    fraction:
        Kelly fraction multiplier. 0.25 (quarter-Kelly) is the default —
        it substantially reduces variance while retaining ~94% of growth rate.
    min_bet:
        Hard floor in USDC per trade (default $5).
    max_bet:
        Hard ceiling in USDC per trade (default $150).
    fee_rate:
        Polymarket maker fee rate deducted from gross proceeds (default 0.002).
    """

    def __init__(
        self,
        fraction: float = 0.25,
        min_bet: float = 5.0,
        max_bet: float = 150.0,
        fee_rate: float = 0.002,
    ) -> None:
        if not (0.0 < fraction <= 1.0):
            raise ValueError(f"fraction must be in (0, 1], got {fraction}")
        self.fraction = fraction
        self.min_bet = min_bet
        self.max_bet = max_bet
        self.fee_rate = fee_rate

    def compute(
        self,
        win_prob: float,
        entry_price: float,
        bankroll: float,
    ) -> float:
        """Compute the dollar amount to bet on a NO-side limit order.

        For a NO contract bought at `entry_price`:
        - Net odds b = (1 - entry_price) / entry_price * (1 - fee_rate)
          (profit per $1 staked when NO resolves to $1, after fees)
        - Kelly fraction f* = (b * p - q) / b
        - Final bet = clamp(fraction * f* * bankroll, min_bet, max_bet)

        Parameters
        ----------
        win_prob:
            Posterior probability of NO winning (from BayesianEngine).
        entry_price:
            NO token limit order price in (0, 1).
        bankroll:
            Current USDC balance available for trading.

        Returns
        -------
        float
            Dollar amount to risk on this trade, or 0.0 if Kelly is negative
            (negative EV — do not trade).
        """
        if not (0.0 < win_prob < 1.0):
            return 0.0
        if not (0.0 < entry_price < 1.0):
            return 0.0
        if bankroll <= 0:
            return 0.0

        p = win_prob
        q = 1.0 - p

        # Net odds: gain per $1 staked when NO wins, after fee
        # We stake entry_price per contract; at resolution we receive $1 per contract
        # so net gain = (1 - entry_price) * (1 - fee_rate) per $entry_price staked
        b = (1.0 - entry_price) / entry_price * (1.0 - self.fee_rate)

        if b <= 0:
            return 0.0

        f_star = (b * p - q) / b

        if f_star <= 0:
            # Negative Kelly → negative EV → skip
            return 0.0

        bet = self.fraction * f_star * bankroll
        return max(self.min_bet, min(self.max_bet, bet))

    def compute_from_edge(
        self,
        edge: float,
        entry_price: float,
        bankroll: float,
    ) -> float:
        """Convenience wrapper: derive win_prob from edge and entry_price.

        edge = calibrated_no_prob - market_no_price
        calibrated_no_prob = market_no_price + edge
        win_prob = calibrated_no_prob = (1 - entry_price) + edge

        Parameters
        ----------
        edge:
            Net edge in probability-point units (from BayesianEngine.get_no_edge).
        entry_price:
            NO token limit order price in (0, 1).
        bankroll:
            Current USDC balance.

        Returns
        -------
        float
            Dollar amount to risk on this trade.
        """
        market_no_price = 1.0 - entry_price
        win_prob = min(0.999, max(0.001, market_no_price + edge))
        return self.compute(win_prob, entry_price, bankroll)
