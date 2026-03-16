"""Prometheus metrics for the bot."""
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Trade metrics
trades_total = Counter("arb_trades_total", "Total trades executed", ["strategy", "side"])
trade_pnl = Histogram("arb_trade_pnl_usdc", "PnL per trade in USDC", ["strategy"],
                       buckets=[-10, -5, -2, -1, 0, 1, 2, 5, 10, 25, 50])

# Portfolio metrics
portfolio_balance = Gauge("arb_portfolio_balance_usdc", "Current portfolio balance in USDC")
portfolio_pnl = Gauge("arb_portfolio_pnl_usdc", "Total unrealized + realized PnL")
open_positions = Gauge("arb_open_positions", "Number of open positions")
total_exposure = Gauge("arb_total_exposure_usdc", "Total notional exposure in USDC")

# Strategy metrics
arb_opportunities = Counter("arb_opportunities_total", "Arb opportunities detected", ["strategy"])
arb_executed = Counter("arb_executed_total", "Arb trades executed", ["strategy"])
edge_detected = Histogram("arb_edge_detected", "Edge size detected", ["strategy"],
                           buckets=[0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5])

# Market making metrics
mm_quotes_placed = Counter("mm_quotes_placed_total", "Market making quotes placed", ["side"])
mm_fills = Counter("mm_fills_total", "Market making fills", ["side"])
mm_inventory = Gauge("mm_inventory_usdc", "Market making inventory in USDC")

# Latency metrics
api_latency = Histogram("arb_api_latency_seconds", "API call latency", ["endpoint"],
                         buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0])
price_lag = Histogram("arb_price_lag_pct", "Price lag vs reference (Binance)",
                       buckets=[0.001, 0.005, 0.01, 0.02, 0.05, 0.1])


def start_metrics_server(port: int = 8000) -> None:
    start_http_server(port)
