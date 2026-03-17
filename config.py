from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))


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
class RiskConfig:
    max_position_size: float = field(default_factory=lambda: _float("MAX_POSITION_SIZE", 500.0))
    max_total_exposure: float = field(default_factory=lambda: _float("MAX_TOTAL_EXPOSURE", 5000.0))
    min_edge_threshold: float = field(default_factory=lambda: _float("MIN_EDGE_THRESHOLD", 0.02))
    max_slippage: float = field(default_factory=lambda: _float("MAX_SLIPPAGE", 0.005))
    max_drawdown_pct: float = 0.15  # Stop trading at 15% drawdown
    max_open_orders: int = 20
    min_trade_interval: int = field(default_factory=lambda: _int("MIN_TRADE_INTERVAL", 60))   # seconds between any two trades
    token_cooldown: int = field(default_factory=lambda: _int("TOKEN_COOLDOWN", 300))           # seconds before re-trading same token


@dataclass
class StrategyConfig:
    rebalancing_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_REBALANCING", True))
    combinatorial_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_COMBINATORIAL", True))
    latency_arb_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_LATENCY_ARB", True))
    market_making_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_MARKET_MAKING", True))

    # Rebalancing
    rebalancing_min_edge: float = 0.02       # Minimum 2c edge after fees
    rebalancing_max_spend: float = 200.0

    # Combinatorial
    combo_min_edge: float = 0.03
    combo_lookback_markets: int = 50

    # Latency arb
    latency_price_lag_threshold: float = 0.015  # 1.5% lag triggers entry
    latency_max_hold_seconds: int = 30

    # Market making
    mm_spread_bps: int = field(default_factory=lambda: _int("MM_SPREAD_BPS", 50))
    mm_order_size: float = field(default_factory=lambda: _float("MM_ORDER_SIZE", 100.0))
    mm_max_inventory: float = field(default_factory=lambda: _float("MM_MAX_INVENTORY", 500.0))
    mm_skew_factor: float = 0.3

    # New strategies
    resolution_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_RESOLUTION", True))
    event_driven_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_EVENT_DRIVEN", True))
    cross_exchange_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_CROSS_EXCHANGE", False))
    futures_hedge_enabled: bool = field(default_factory=lambda: _bool("STRATEGY_FUTURES_HEDGE", False))

    # Markets coverage
    max_markets: int = field(default_factory=lambda: _int("MAX_MARKETS", 500))
    max_days_to_resolution: int = field(default_factory=lambda: _int("MAX_DAYS_TO_RESOLUTION", 30))

    # WebSocket orderbook feed
    use_ws_orderbook: bool = field(default_factory=lambda: _bool("USE_WS_ORDERBOOK", True))


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
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategies: StrategyConfig = field(default_factory=StrategyConfig)


CONFIG = BotConfig()
