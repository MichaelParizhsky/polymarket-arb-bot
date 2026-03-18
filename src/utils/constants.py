"""
Shared constants used across strategy modules.
Single source of truth — change here to affect all strategies.
"""

# Polymarket taker fee per side (0.2%)
FEE_RATE: float = 0.002

# Estimated slippage per side
SLIPPAGE_RATE: float = 0.002

# Combined round-trip cost (fee both sides)
ROUND_TRIP_COST: float = 2 * FEE_RATE
