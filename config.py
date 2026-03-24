from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _float(key: str, default: float) -> float:
    val = os.getenv(key, str(default)).strip()
    try:
        return float(val)
    except ValueError:
        return default


def _int(key: str, default: int) -> int:
    val = os.getenv(key, str(default)).strip()
    try:
        return int(val)
    except ValueError:
        return default


@dataclass
class PolymarketConfig:
    api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE", ""))
    private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))
    funder_address: str = field(default_factory=lambda: os.getenv("POLYMARKET_FUNDER_ADDRESS", ""))
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137  # Polygon mainnet


@dataclass
class BinanceConfig:
    api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_API_SECRET", ""))
    ws_url: str = field(default_factory=lambda: os.getenv("BINANCE_WS_URL", "wss://stream.binance.us:9443/ws"))
    rest_url: str = field(default_factory=lambda: os.getenv("BINANCE_REST_URL", "https://api.binance.us"))
    futures_enabled: bool = field(default_factory=lambda: _bool("BINANCE_FUTURES_ENABLED", False))


@dataclass
class KalshiConfig:
    api_token: str = field(default_factory=lambda: os.getenv("KALSHI_API_TOKEN", ""))
    email: str = field(default_factory=lambda: os.getenv("KALSHI_EMAIL", ""))
    password: str = field(default_factory=lambda: os.getenv("KALSHI_PASSWORD", ""))
    base_url: str = "https://trading-api.kalshi.com/trade-api/v2"
    enabled: bool = field(default_factory=lambda: _bool("KALSHI_ENABLED", False))


@dataclass
class AIResearchConfig:
    """API keys for real-time AI research integrations."""
    perplexity_api_key: str = field(default_factory=lambda: os.getenv("PERPLEXITY_API_KEY", ""))
    grok_api_key: str = field(default_factory=lambda: os.getenv("GROK_API_KEY", ""))
    mirofish_url: str = field(default_factory=lambda: os.getenv("MIROFISH_URL", ""))


@dataclass
class RiskConfig:
    max_position_size: float = field(default_factory=lambda: _float("MAX_POSITION_SIZE", 500.0))
    max_total_exposure: float = field(default_factory=lambda: _float("MAX_TOTAL_EXPOSURE", 5000.0))
    min_edge_threshold: float = field(default_factory=lambda: _float("MIN_EDGE_THRESHOLD", 0.02))
    max_slippage: float = field(default_factory=lambda: _float("MAX_SLIPPAGE", 0.005))
    max_drawdown_pct: float = field(default_factory=lambda: _float("MAX_DRAWDOWN_PCT", 0.15))
    max_open_orders: int = field(default_factory=lambda: _int("MAX_OPEN_ORDERS", 20))
    min_trade_interval: int = field(default_factory=lambda: _int("MIN_TRADE_INTERVAL", 15))   # seconds between any two trades (Polymarket allows 3500 orders/10s)
    token_cooldown: int = field(default_factory=lambda: _int("TOKEN_COOLDOWN", 120))           # seconds before re-trading same token
    hard_stop_max_count: int = 3
    hard_stop_window_hours: int = 24
    strategy_loss_budget: dict = field(default_factory=lambda: {
        "rebalancing": 300.0,
        "combinatorial": 400.0,
        "market_making": 200.0,
        "resolution": 600.0,
        "event_driven": 300.0,
        "cross_exchange": 500.0,
        "quick_resolution": 400.0,
        "futures_hedge": 200.0,
        "crypto_5m": 150.0,  # tight budget — snipe mode fires blind without Binance data
        "swarm_prediction": 300.0,  # crowd simulation strategy budget
        "auto_close": 9999.0,  # auto-close is resolution, not a strategy — no budget cap
    })


@dataclass
class StrategyConfig:
    rebalancing_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_REBALANCING", True))
    combinatorial_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_COMBINATORIAL", True))
    latency_arb_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_LATENCY_ARB", False))  # disabled: Polymarket dynamic fees killed this strategy
    market_making_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_MARKET_MAKING", True))

    # Rebalancing — must clear 2% winner fee + gas; set above 2%
    rebalancing_min_edge: float = field(default_factory=lambda: _float("REBALANCING_MIN_EDGE", 0.025))
    rebalancing_max_spend: float = 200.0

    # Combinatorial
    combo_min_edge: float = field(default_factory=lambda: _float("COMBO_MIN_EDGE", 0.03))
    combo_lookback_markets: int = field(default_factory=lambda: _int("COMBO_LOOKBACK_MARKETS", 50))

    # Latency arb (mostly disabled — Polymarket dynamic fees make this fee-negative at 50/50)
    latency_price_lag_threshold: float = 0.015  # 1.5% lag triggers entry
    latency_max_hold_seconds: int = 30

    # Market making
    mm_spread_bps: int = field(default_factory=lambda: _int("MM_SPREAD_BPS", 50))
    mm_min_spread_bps: int = field(default_factory=lambda: _int("MM_MIN_SPREAD_BPS", 10))       # never quote tighter than 10 bps
    mm_order_size: float = field(default_factory=lambda: _float("MM_ORDER_SIZE", 100.0))
    mm_max_inventory: float = field(default_factory=lambda: _float("MM_MAX_INVENTORY", 500.0))
    mm_skew_factor: float = field(default_factory=lambda: _float("MM_SKEW_FACTOR", 0.3))
    mm_inventory_skew_limit: float = field(default_factory=lambda: _float("MM_INVENTORY_SKEW_LIMIT", 0.30))  # stop quoting adverse side at 30% skew
    mm_max_market_spread_pct: float = field(default_factory=lambda: _float("MM_MAX_MARKET_SPREAD_PCT", 0.06))  # skip markets with >6% bid-ask spread

    # Cross-exchange — separate min edge (needs to clear ~4-5% combined fees)
    cross_exchange_min_edge: float = field(default_factory=lambda: _float("CROSS_EXCHANGE_MIN_EDGE", 0.05))
    cross_exchange_safe_only: bool = field(default_factory=lambda: _bool("CROSS_EXCHANGE_SAFE_ONLY", True))  # only trade mechanical-resolution markets

    # Futures hedge ratio for crypto market positions
    hedge_ratio: float = field(default_factory=lambda: _float("HEDGE_RATIO", 0.3))  # 30% of position size

    # New strategies
    resolution_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_RESOLUTION", True))
    event_driven_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_EVENT_DRIVEN", False))
    cross_exchange_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_CROSS_EXCHANGE", False))
    futures_hedge_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_FUTURES_HEDGE", False))

    # Quick resolution: targets markets resolving within a few hours (5-min, 15-min crypto, sports)
    # Faster capital recycling — high conviction entries at price extremes
    quick_resolution_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_QUICK_RESOLUTION", True))
    quick_resolution_max_hours: float = field(default_factory=lambda: float(os.getenv("QUICK_RESOLUTION_MAX_HOURS", "24")))
    quick_resolution_min_conviction: float = field(default_factory=lambda: _float("QUICK_RESOLUTION_MIN_CONVICTION", 0.78))
    # Floor applied to tiered conviction reductions (set low in paper mode for more trades)
    quick_resolution_conviction_floor: float = field(default_factory=lambda: _float("QUICK_RESOLUTION_CONVICTION_FLOOR", 0.68))
    quick_resolution_min_edge: float = field(default_factory=lambda: _float("QUICK_RESOLUTION_MIN_EDGE", 0.010))
    quick_resolution_min_volume: float = field(default_factory=lambda: _float("QUICK_RESOLUTION_MIN_VOLUME", 100.0))
    quick_resolution_max_spend: float = field(default_factory=lambda: _float("QUICK_RESOLUTION_MAX_SPEND", 150.0))
    # Re-entry cooldown per market (hours). Default 12h; set to 0.5 in paper mode for frequent re-entry.
    quick_resolution_entered_hours: float = field(default_factory=lambda: _float("QUICK_RESOLUTION_ENTERED_HOURS", 12.0))

    # Crypto 5m/15m short market strategy
    crypto_5m_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_CRYPTO_5M", True))
    crypto_5m_max_spend: float = field(default_factory=lambda: float(os.getenv("CRYPTO_5M_MAX_SPEND", "100.0")))
    crypto_5m_coins: list = field(default_factory=lambda: ["btc", "eth", "sol"])

    # Markets coverage
    max_markets: int = field(default_factory=lambda: _int("MAX_MARKETS", 500))
    max_days_to_resolution: int = field(default_factory=lambda: _int("MAX_DAYS_TO_RESOLUTION", 30))

    # WebSocket orderbook feed
    use_ws_orderbook: bool = field(default_factory=lambda: _bool("USE_WS_ORDERBOOK", True))

    # Swarm prediction strategy (MiroFish-inspired crowd simulation)
    # Disabled by default — requires PERPLEXITY_API_KEY to generate signals.
    # With MIROFISH_URL set, uses the full Node.js MiroFish engine.
    # Without it, falls back to multi-persona LLM simulation (pure Python).
    swarm_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_SWARM", False))
    swarm_min_edge: float = field(default_factory=lambda: _float("SWARM_MIN_EDGE", 0.05))
    swarm_min_confidence: float = field(default_factory=lambda: _float("SWARM_MIN_CONFIDENCE", 0.65))
    swarm_max_spend: float = field(default_factory=lambda: _float("SWARM_MAX_SPEND", 150.0))
    swarm_max_markets_per_cycle: int = field(default_factory=lambda: _int("SWARM_MAX_MARKETS", 5))
    swarm_min_volume: float = field(default_factory=lambda: _float("SWARM_MIN_VOLUME", 5000.0))
    swarm_agent_count: int = field(default_factory=lambda: _int("SWARM_AGENT_COUNT", 12))
    swarm_cooldown_hours: float = field(default_factory=lambda: _float("SWARM_COOLDOWN_HOURS", 4.0))


@dataclass
class BotConfig:
    paper_trading: bool = field(default_factory=lambda: _bool("PAPER_TRADING", True))
    starting_balance: float = field(default_factory=lambda: _float("STARTING_BALANCE", 10000.0))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    market_poll_interval: int = field(default_factory=lambda: _int("MARKET_POLL_INTERVAL", 5))
    orderbook_poll_interval: int = field(default_factory=lambda: _int("ORDERBOOK_POLL_INTERVAL", 1))
    binance_ws_reconnect_delay: int = field(default_factory=lambda: _int("BINANCE_WS_RECONNECT_DELAY", 5))
    metrics_port: int = 8000

    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    ai_research: AIResearchConfig = field(default_factory=AIResearchConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategies: StrategyConfig = field(default_factory=StrategyConfig)


CONFIG = BotConfig()
