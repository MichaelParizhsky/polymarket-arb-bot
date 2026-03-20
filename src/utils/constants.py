"""
Shared constants used across strategy modules.
Single source of truth — change here to affect all strategies.
"""

# Polymarket standard taker fee per side (0.2% flat for most markets)
FEE_RATE: float = 0.002

# Estimated slippage per side
SLIPPAGE_RATE: float = 0.002

# Combined round-trip cost (fee both sides, standard markets)
ROUND_TRIP_COST: float = 2 * FEE_RATE

# Maker rebate: 20% of taker fees collected are returned daily
MAKER_REBATE_PCT: float = 0.20

# Minimum viable trade size in USDC (no exchange floor, but sub-$1 not worth the gas)
MIN_TRADE_USDC: float = 1.0


def calc_taker_fee(price: float, market_type: str = "standard") -> float:
    """
    Calculate the taker fee for a given price and market type.

    Polymarket uses dynamic (non-linear) fees on short-duration crypto markets
    to deter latency arbitrage. The fee peaks at 50% probability and approaches
    zero at the extremes.

    Formula: fee = feeRate * (price * (1 - price)) ^ exponent

    Market types:
        "standard"  — flat 0.2% (most markets, politics, elections)
        "crypto_5m" — 5-min/15-min crypto markets: feeRate=0.25, exp=2 → max 1.56% at 50%
        "sports"    — flat 0.30% (confirmed Feb 2026: NCAAB, soccer, NFL, etc.)
        "dcm"       — US regulated DCM: flat 0.30%

    See: https://docs.polymarket.com/trading/fees
    """
    p = max(0.001, min(0.999, price))  # clamp to avoid math edge cases
    if market_type == "crypto_5m":
        return 0.25 * (p * (1.0 - p)) ** 2
    if market_type == "sports":
        return 0.003  # flat 30 bps — confirmed Feb 2026 (NCAAB, soccer, etc.)
    if market_type == "dcm":
        return 0.003
    # standard / default
    return FEE_RATE


def net_edge_after_fee(gross_edge: float, entry_price: float, market_type: str = "standard") -> float:
    """Gross edge minus entry fee. Convenience wrapper."""
    return gross_edge - calc_taker_fee(entry_price, market_type)
