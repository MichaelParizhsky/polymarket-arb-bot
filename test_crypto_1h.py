#!/usr/bin/env python3
"""
Local test script for the crypto_1h strategy.

Runs in PAPER mode — no real orders placed.
Tests:
  1. Market discovery via get_crypto_1h_markets()
  2. Orderbook fetch for discovered tokens
  3. Snipe signal logic (simulated with mock hour opens)

Usage:
    python test_crypto_1h.py

Expected output:
  - List of discovered markets per coin
  - Orderbook data (ask prices, spreads)
  - Simulated snipe signals if market is within snipe window
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

# Force paper mode
os.environ.setdefault("PAPER_TRADING", "True")
os.environ.setdefault("STRATEGY_CRYPTO_1H", "True")
os.environ.setdefault("STRATEGY_CRYPTO_5M", "False")
os.environ.setdefault("STRATEGY_COMBINATORIAL", "False")
os.environ.setdefault("STRATEGY_MARKET_MAKING", "False")
os.environ.setdefault("STRATEGY_RESOLUTION", "False")
os.environ.setdefault("STRATEGY_EVENT_DRIVEN", "False")

from config import CONFIG
from src.exchange.polymarket import PolymarketClient
from src.strategies.crypto_1h import (
    CONFIDENCE_TABLE, MIN_CANDLE_MOVE, MIN_NET_EDGE, SNIPE_WINDOW_SECONDS,
    _taker_fee, _win_probability, _seconds_to_et_hour_end, _resolve_up_down_tokens,
    _coin_from_question, Crypto1hStrategy, ET_OFFSET,
)

ET_OFFSET_DELTA = timedelta(hours=-4)


def print_header(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_fee_table() -> None:
    print_header("Fee Table: crypto_fees_v2 (exponent=1)")
    print(f"  {'Price (p)':>12} | {'Taker Fee':>12} | {'Maker Fee':>12}")
    print(f"  {'-'*42}")
    for p in [0.50, 0.60, 0.70, 0.80, 0.88, 0.90, 0.95, 0.96, 0.99]:
        fee = _taker_fee(p)
        print(f"  {p:>12.2f} | {fee:>11.4%} | {'0.00%':>12}")


def print_edge_table() -> None:
    print_header("Net Edge Table: P(win) - ask - fee")
    print(f"  Confidence table:")
    for threshold, prob in CONFIDENCE_TABLE:
        fee_at_p = _taker_fee(0.70)  # typical market price when we'd fire
        print(f"    candle >= {threshold:.1%} -> P(win) = {prob:.0%}")

    print()
    print(f"  {'Candle':>8} | {'P(win)':>8} | {'Ask':>6} | {'Fee':>7} | {'Edge':>8} | {'Fire?':>6}")
    print(f"  {'-'*56}")
    for candle_abs in [0.007, 0.010, 0.015, 0.020, 0.030]:
        prob = _win_probability(candle_abs)
        for ask in [0.50, 0.60, 0.70, 0.75, 0.80]:
            fee = _taker_fee(ask)
            edge = prob - ask - fee
            fire = "YES" if edge >= MIN_NET_EDGE else "no"
            print(f"  {candle_abs:>7.1%} | {prob:>8.0%} | {ask:>6.2f} | {fee:>7.4%} | {edge:>8.4f} | {fire:>6}")
        print()


async def test_market_discovery(client: PolymarketClient) -> list:
    print_header("Market Discovery")
    coins = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
    print(f"  Fetching 1h markets for: {coins}")
    markets = await client.get_crypto_1h_markets(coins=coins, include_next_hours=2)
    print(f"  Found {len(markets)} markets\n")
    for m in markets:
        up, down = _resolve_up_down_tokens(m)
        coin = _coin_from_question(m.question)
        active_str = "ACTIVE" if m.active and not m.closed else "inactive"
        print(f"  [{active_str}] {coin or '?':>4} | {m.question[:55]}")
        print(f"         end={m.end_date_iso}  cid={m.condition_id[:16]}...")
        if up and down:
            print(f"         Up token={up.token_id[:12]}...  Down token={down.token_id[:12]}...")
    return markets


async def test_orderbooks(client: PolymarketClient, markets: list) -> dict:
    print_header("Orderbook Data")
    token_ids = []
    for m in markets:
        for t in m.tokens:
            token_ids.append(t.token_id)

    if not token_ids:
        print("  No tokens to fetch")
        return {}

    print(f"  Fetching {len(token_ids)} orderbooks...")
    tasks = [client.get_orderbook(tid) for tid in token_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    orderbooks = {}
    for tid, res in zip(token_ids, results):
        if isinstance(res, Exception):
            print(f"  ERROR {tid[:12]}...: {res}")
        else:
            orderbooks[tid] = res

    print(f"  Got {len(orderbooks)}/{len(token_ids)} orderbooks\n")
    for m in markets[:4]:  # show first 4 markets
        up, down = _resolve_up_down_tokens(m)
        if not up or not down:
            continue
        up_book = orderbooks.get(up.token_id)
        dn_book = orderbooks.get(down.token_id)
        coin = _coin_from_question(m.question) or "?"
        print(f"  {coin.upper()} | {m.question[:45]}")
        if up_book:
            print(f"    Up:   bid={up_book.best_bid}  ask={up_book.best_ask}  mid={up_book.mid}")
        if dn_book:
            print(f"    Down: bid={dn_book.best_bid}  ask={dn_book.best_ask}  mid={dn_book.mid}")
    return orderbooks


def test_snipe_logic(markets: list, orderbooks: dict) -> None:
    print_header("Snipe Signal Simulation")
    now_et = datetime.now(timezone.utc) + ET_OFFSET_DELTA
    secs_left = _seconds_to_et_hour_end(now_et)
    et_hour = now_et.hour
    print(f"  Current ET time: {now_et.strftime('%H:%M:%S')} ET")
    print(f"  ET hour: {et_hour:02d}:00  Seconds to end: {secs_left:.0f}s")
    in_snipe_window = secs_left <= SNIPE_WINDOW_SECONDS
    print(f"  Snipe window ({SNIPE_WINDOW_SECONDS:.0f}s): {'YES - firing zone' if in_snipe_window else 'NO - too early'}")
    print()

    # Simulate different candle scenarios
    scenarios = [
        ("BTC +1.5% candle (10 min left)", "btc", 0.015, secs_left if in_snipe_window else 580.0),
        ("BTC +0.5% candle (10 min left)", "btc", 0.005, 580.0),  # below min_candle_move
        ("ETH -0.8% candle (5 min left)", "eth", -0.008, 300.0),
        ("SOL +2.0% candle (2 min left)", "sol", 0.020, 120.0),
    ]

    for desc, coin, candle_return, sim_secs_left in scenarios:
        candle_abs = abs(candle_return)
        print(f"  Scenario: {desc}")
        if candle_abs < MIN_CANDLE_MOVE:
            print(f"    -> SKIP: candle {candle_abs:.2%} < min {MIN_CANDLE_MOVE:.2%}")
            print()
            continue

        # Find matching active market
        mkt = None
        for m in markets:
            if not m.active or m.closed:
                continue
            if _coin_from_question(m.question) == coin:
                mkt = m
                break

        if not mkt:
            print(f"    -> No active {coin.upper()} market found in discovered set")
            print()
            continue

        up_token, down_token = _resolve_up_down_tokens(mkt)
        if not up_token or not down_token:
            print(f"    -> Could not resolve Up/Down tokens")
            print()
            continue

        direction = "UP" if candle_return > 0 else "DOWN"
        entry_token = up_token if direction == "UP" else down_token
        entry_book = orderbooks.get(entry_token.token_id)

        if not entry_book or entry_book.best_ask is None:
            print(f"    -> No orderbook for {direction} token")
            print()
            continue

        ask = entry_book.best_ask
        win_prob = _win_probability(candle_abs)
        fee = _taker_fee(ask)
        net_edge = win_prob - ask - fee
        fire = net_edge >= MIN_NET_EDGE

        print(f"    direction={direction}  candle={candle_return:+.4%}  P(win)={win_prob:.2f}")
        print(f"    ask={ask:.4f}  fee={fee:.4f}  net_edge={net_edge:.4f}")
        print(f"    -> {'FIRE SIGNAL' if fire else 'SKIP (edge too low)'}")
        print()


async def main() -> None:
    print_fee_table()
    print_edge_table()

    async with PolymarketClient(CONFIG) as client:
        markets = await test_market_discovery(client)
        if not markets:
            print("\nNo markets found — cannot continue orderbook/snipe tests")
            return
        orderbooks = await test_orderbooks(client, markets)
        test_snipe_logic(markets, orderbooks)

    print_header("Summary")
    active = [m for m in markets if m.active and not m.closed]
    print(f"  Total markets discovered: {len(markets)}")
    print(f"  Active markets: {len(active)}")
    print(f"  Orderbooks fetched: {len(orderbooks)}")
    print(f"\n  Strategy is ready. Enable with: STRATEGY_CRYPTO_1H=True")
    print(f"  Current state: PAPER MODE (no real orders)\n")


if __name__ == "__main__":
    asyncio.run(main())
