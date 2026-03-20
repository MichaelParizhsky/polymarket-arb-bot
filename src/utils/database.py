"""
SQLite-backed persistence layer for trades, signals, parameters, and positions.
Thread-safe via connection-per-call pattern.
"""
from __future__ import annotations

import sqlite3
import time
import json
import os
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

from src.utils.logger import logger

DB_PATH = os.environ.get("DB_PATH", "logs/polybot.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist. Call once at startup."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                strategy TEXT NOT NULL,
                market_id TEXT,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                usdc_amount REAL NOT NULL,
                price REAL NOT NULL,
                contracts REAL NOT NULL,
                fee REAL NOT NULL,
                slippage REAL NOT NULL,
                is_paper INTEGER NOT NULL DEFAULT 1,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                strategy TEXT NOT NULL,
                market_id TEXT,
                token_id TEXT,
                signal_type TEXT NOT NULL,
                estimated_edge REAL NOT NULL,
                executed INTEGER NOT NULL DEFAULT 0,
                execution_price REAL,
                resolution_price REAL,
                pnl REAL,
                reject_reason TEXT,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS parameters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                strategy TEXT,
                param_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                proposed_by TEXT,
                validated_at REAL,
                shadow_improvement REAL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS strategy_metrics_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                strategy TEXT NOT NULL,
                trade_count INTEGER,
                win_count INTEGER,
                total_pnl REAL,
                win_rate REAL,
                avg_edge REAL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy);
            CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
            CREATE INDEX IF NOT EXISTS idx_parameters_status ON parameters(status);
        """)
    logger.info(f"Database initialized at {DB_PATH}")


def log_trade(
    strategy: str,
    token_id: str,
    side: str,
    usdc_amount: float,
    price: float,
    contracts: float,
    fee: float,
    slippage: float,
    market_id: str = "",
    is_paper: bool = True,
    metadata: Optional[dict] = None,
) -> int:
    """Log an executed trade. Returns the row id."""
    with _get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO trades
               (timestamp, strategy, market_id, token_id, side, usdc_amount, price, contracts, fee, slippage, is_paper, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(), strategy, market_id, token_id, side,
                usdc_amount, price, contracts, fee, slippage,
                1 if is_paper else 0,
                json.dumps(metadata) if metadata else None,
            ),
        )
        return cursor.lastrowid


def log_signal(
    strategy: str,
    signal_type: str,
    estimated_edge: float,
    executed: bool = False,
    token_id: str = "",
    market_id: str = "",
    execution_price: Optional[float] = None,
    reject_reason: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    """Log a generated signal (executed or rejected). Returns row id."""
    with _get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO signals
               (timestamp, strategy, market_id, token_id, signal_type, estimated_edge, executed, execution_price, reject_reason, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(), strategy, market_id, token_id, signal_type,
                estimated_edge, 1 if executed else 0,
                execution_price, reject_reason,
                json.dumps(metadata) if metadata else None,
            ),
        )
        return cursor.lastrowid


def resolve_signal(signal_id: int, resolution_price: float, pnl: float) -> None:
    """Update a signal with its final resolution outcome."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE signals SET resolution_price=?, pnl=? WHERE id=?",
            (resolution_price, pnl, signal_id),
        )


def propose_parameter_change(
    param_name: str,
    old_value,
    new_value,
    strategy: Optional[str] = None,
    proposed_by: str = "meta_agent",
    notes: Optional[str] = None,
) -> int:
    """Record a proposed parameter change. Returns row id."""
    with _get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO parameters
               (timestamp, strategy, param_name, old_value, new_value, status, proposed_by, notes)
               VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)""",
            (
                time.time(), strategy, param_name,
                json.dumps(old_value), json.dumps(new_value),
                proposed_by, notes,
            ),
        )
        return cursor.lastrowid


def validate_parameter_change(param_id: int, shadow_improvement: float) -> None:
    """Mark a proposed parameter change as validated (ready to apply)."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE parameters SET status='validated', validated_at=?, shadow_improvement=? WHERE id=?",
            (time.time(), shadow_improvement, param_id),
        )


def apply_parameter_change(param_id: int) -> None:
    """Mark a parameter change as applied (live)."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE parameters SET status='active' WHERE id=?",
            (param_id,),
        )


def rollback_parameter_change(param_id: int) -> None:
    """Mark a parameter change as rolled back."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE parameters SET status='rolled_back' WHERE id=?",
            (param_id,),
        )


def get_pending_parameter_proposals() -> list[dict]:
    """Return all parameter changes with status='proposed'."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM parameters WHERE status='proposed' ORDER BY timestamp DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_strategy_metrics(strategy: str, since_hours: float = 24.0) -> dict:
    """Compute live metrics for a strategy from the trades table."""
    since_ts = time.time() - since_hours * 3600
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT side, usdc_amount, price, fee, contracts
               FROM trades WHERE strategy=? AND timestamp>=?""",
            (strategy, since_ts),
        ).fetchall()

    if not rows:
        return {"trade_count": 0, "total_pnl": 0.0, "win_rate": 0.0}

    trade_count = len(rows)
    # Approximate PnL: sells return USDC, buys cost USDC (minus fees)
    total_pnl = sum(
        (r["usdc_amount"] - r["fee"]) if r["side"].upper() == "SELL"
        else -(r["usdc_amount"] + r["fee"])
        for r in rows
    )
    return {
        "trade_count": trade_count,
        "total_pnl": round(total_pnl, 4),
        "win_rate": 0.0,  # requires resolution data
    }


def get_recent_signals(strategy: str, limit: int = 50) -> list[dict]:
    """Return most recent signals for a strategy."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE strategy=? ORDER BY timestamp DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_signal_quality(strategy: str, since_hours: float = 168.0) -> dict:
    """Compute signal quality: what % of executed signals were profitable."""
    since_ts = time.time() - since_hours * 3600
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT pnl FROM signals
               WHERE strategy=? AND timestamp>=? AND executed=1 AND pnl IS NOT NULL""",
            (strategy, since_ts),
        ).fetchall()

    if not rows:
        return {"signal_count": 0, "win_rate": 0.0, "avg_pnl": 0.0}

    pnls = [r["pnl"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "signal_count": len(pnls),
        "win_rate": round(wins / len(pnls), 4),
        "avg_pnl": round(sum(pnls) / len(pnls), 4),
    }
