"""
settle_positions.py — Auto-close resolved Polymarket positions.

Runs every 5 minutes at :01, :06, :11, :16, :21 ... (UTC).

For each position in portfolio_state.json:
  1. Queries data-api.polymarket.com/positions to see what's still live on-chain
  2. Any token no longer in live positions = market has resolved
  3. Checks gamma-api to determine YES/NO outcome
  4. Updates portfolio_state.json with realized PnL and removes closed positions
  5. Redeemable (won) positions are auto-redeemed on-chain via Polymarket relayer
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
STATE_FILE   = Path(os.getenv("STATE_FILE", "logs/portfolio_state.json"))
FUNDER_ADDR  = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
DATA_API     = "https://data-api.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
INTERVAL     = 5 * 60          # 300 seconds between runs
OFFSET       = 60              # :01 past each 5-minute boundary

# ── Redemption credentials (builder API — may match CLOB credentials) ─────────
# Register at polymarket.com/settings?tab=builder if not enrolled yet.
# Falls back to CLOB credentials if POLYMARKET_BUILDER_* not set.
_BUILDER_KEY        = (os.getenv("POLYMARKET_BUILDER_API_KEY")
                       or os.getenv("POLYMARKET_API_KEY", ""))
_BUILDER_SECRET     = (os.getenv("POLYMARKET_BUILDER_SECRET")
                       or os.getenv("POLYMARKET_API_SECRET", ""))
_BUILDER_PASSPHRASE = (os.getenv("POLYMARKET_BUILDER_PASSPHRASE")
                       or os.getenv("POLYMARKET_API_PASSPHRASE", ""))
_PRIVATE_KEY        = os.getenv("POLYMARKET_PRIVATE_KEY", "")
# signature_type: 1 = proxy/email wallet, 0/2 = EOA/Safe (builder)
_SIGNATURE_TYPE     = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))

# Polygon contract addresses
_CTF_ADDRESS      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
_USDC_ADDRESS     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# ─────────────────────────────────────────────────────────────────────────────


# ── On-chain redemption ───────────────────────────────────────────────────────
def _build_relay_client():
    """
    Build a Polymarket Builder Relayer client.
    Returns None if credentials are missing or library not installed.
    """
    if not all([_BUILDER_KEY, _BUILDER_SECRET, _BUILDER_PASSPHRASE, _PRIVATE_KEY]):
        print("  [WARN] Builder credentials not fully set. Cannot auto-redeem.")
        print("  Set POLYMARKET_BUILDER_API_KEY/SECRET/PASSPHRASE")
        print("  Or enroll at polymarket.com/settings?tab=builder")
        return None
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import RelayerTxType
        from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

        wallet_type = (
            RelayerTxType.PROXY if _SIGNATURE_TYPE == 1 else RelayerTxType.SAFE
        )
        return RelayClient(
            "https://relayer-v2.polymarket.com",
            chain_id=137,
            private_key=_PRIVATE_KEY,
            builder_config=BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=_BUILDER_KEY,
                    secret=_BUILDER_SECRET,
                    passphrase=_BUILDER_PASSPHRASE,
                )
            ),
            relay_tx_type=wallet_type,
        )
    except ImportError:
        print("  [WARN] py-builder-relayer-client not installed.")
        print("  Add to requirements.txt:")
        print("    git+https://github.com/Polymarket/py-builder-relayer-client.git")
        print("    git+https://github.com/Polymarket/py-builder-signing-sdk.git")
        return None
    except Exception as exc:
        print(f"  [WARN] RelayClient init failed: {exc}")
        return None


def _redeem_position_sync(relay_client, live_pos: dict) -> bool:
    """
    Submit on-chain redeemPositions call via Polymarket relayer (synchronous).
    Returns True if the redemption transaction was confirmed.
    """
    from eth_abi import encode as eth_encode
    from eth_utils import keccak
    from py_builder_relayer_client.models import OperationType, SafeTransaction

    REDEEM_SEL = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    NEG_RISK_SEL = keccak(text="redeemPositions(bytes32,uint256[])")[:4]

    cid = live_pos.get("conditionId", live_pos.get("condition_id", ""))
    if not cid:
        return False
    if not cid.startswith("0x"):
        cid = "0x" + cid

    condition_bytes = bytes.fromhex(cid[2:])
    neg_risk = live_pos.get("negativeRisk")

    try:
        if neg_risk is True:
            # neg-risk: redeemPositions(bytes32 conditionId, uint256[] amounts)
            size_raw = int(float(live_pos.get("size", 0)) * 1e6)
            outcome_index = int(live_pos.get("outcomeIndex", 0))
            amounts = [0, 0]
            amounts[outcome_index] = size_raw
            args = eth_encode(["bytes32", "uint256[]"], [condition_bytes, amounts])
            txn = SafeTransaction(
                to=_NEG_RISK_ADAPTER,
                operation=OperationType.Call,
                data="0x" + (NEG_RISK_SEL + args).hex(),
                value="0",
            )
        else:
            # standard: redeemPositions(address, bytes32, bytes32, uint256[])
            args = eth_encode(
                ["address", "bytes32", "bytes32", "uint256[]"],
                [_USDC_ADDRESS, b"\x00" * 32, condition_bytes, [1, 2]],
            )
            txn = SafeTransaction(
                to=_CTF_ADDRESS,
                operation=OperationType.Call,
                data="0x" + (REDEEM_SEL + args).hex(),
                value="0",
            )

        resp = relay_client.execute([txn], f"redeem {cid[:12]}")
        resp.wait()
        return True
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status in (429, 1015):
            print(f"  [WARN] Relayer rate limited, will retry next cycle")
        else:
            print(f"  [WARN] Relay error: {exc}")
        return False


# ── Scheduling ────────────────────────────────────────────────────────────────
def seconds_until_next_run() -> float:
    """
    Returns seconds until next :01, :06, :11, :16 ... mark.
    Pattern: second_of_hour ≡ 60 (mod 300).
    """
    now = datetime.now(timezone.utc)
    second_of_hour = now.minute * 60 + now.second
    position = (second_of_hour - OFFSET) % INTERVAL
    if position < 0:
        position += INTERVAL
    if position == 0:
        return 0.0
    return float(INTERVAL - position)
# ─────────────────────────────────────────────────────────────────────────────


# ── API helpers ───────────────────────────────────────────────────────────────
async def fetch_live_positions(client: httpx.AsyncClient) -> list[dict]:
    """
    Returns all on-chain positions for the funder wallet.
    Each dict contains: asset (token_id), title, outcome, size, avgPrice,
    curPrice, redeemable, endDate, cashPnl, percentPnl.
    """
    if not FUNDER_ADDR:
        return []
    try:
        resp = await client.get(
            f"{DATA_API}/positions",
            params={"user": FUNDER_ADDR, "sizeThreshold": "0.01"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        print(f"  [WARN] fetch_live_positions: {exc}")
        return []


async def fetch_market_outcome(
    condition_id: str, client: httpx.AsyncClient, token_id: str = ""
) -> dict | None:
    """
    Returns gamma-api market dict for a condition_id.
    Key fields: resolved (bool), resolvedPrice (0.0 or 1.0), question.
    Falls back to token_id lookup if condition_id is empty.
    """
    if not condition_id and not token_id:
        return None

    # Primary: look up by condition_id
    if condition_id:
        try:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={"condition_id": condition_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data if isinstance(data, list) else [data]
            return markets[0] if markets else None
        except Exception as exc:
            print(f"  [WARN] fetch_market_outcome({condition_id[:14]}): {exc}")
            return None

    # Fallback: look up by token_id (for positions saved before condition_id was stored)
    try:
        resp = await client.get(
            f"{GAMMA_API}/markets",
            params={"clob_token_ids": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else [data]
        return markets[0] if markets else None
    except Exception as exc:
        print(f"  [WARN] fetch_market_outcome(token={token_id[:14]}): {exc}")
        return None
# ─────────────────────────────────────────────────────────────────────────────


# ── State I/O ─────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict) -> None:
    """Atomic write: write to .tmp then rename so the bot never reads a partial file."""
    tmp = STATE_FILE.with_suffix(".settle_tmp")
    STATE_FILE.with_suffix(".settle_bak").unlink(missing_ok=True)
    shutil.copy(STATE_FILE, STATE_FILE.with_suffix(".settle_bak"))
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)
# ─────────────────────────────────────────────────────────────────────────────


# ── Core settlement ───────────────────────────────────────────────────────────
async def run_settlement() -> None:
    state = load_state()
    if not state:
        print("  [SKIP] portfolio_state.json not found or empty.")
        return

    positions: dict = state.get("positions", {})
    if not positions:
        print("  [OK] No open positions.")
        return

    now_ts = time.time()
    dirty = False

    # Build relay client once per run (avoids repeated import overhead)
    relay_client = _build_relay_client()
    loop = asyncio.get_event_loop()

    async with httpx.AsyncClient() as client:
        # ── Step 1: fetch what's actually live on-chain ───────────────────
        live = await fetch_live_positions(client)
        live_token_ids  = {p["asset"] for p in live}
        live_redeemable = {p["asset"] for p in live if p.get("redeemable")}
        live_by_token   = {p["asset"]: p for p in live}

        print(f"  Live on-chain: {len(live)} positions  |  redeemable: {len(live_redeemable)}")

        wins = losses = skipped = 0

        for token_id, pos in list(positions.items()):
            question = pos.get("market_question", "?")[:55]
            contracts = pos.get("contracts", 0.0)
            avg_cost  = pos.get("avg_cost", 0.0)
            outcome   = pos.get("outcome", "YES").upper()
            cost_basis = contracts * avg_cost
            condition_id = pos.get("condition_id", "")

            # ── Redeemable: won and waiting for collection ─────────────────
            if token_id in live_redeemable:
                cur_price = live_by_token[token_id].get("curPrice", 1.0)
                payout    = contracts * cur_price
                pnl       = payout - cost_basis
                print(f"  [REDEEMING WIN] {question}")
                print(f"    {contracts:.1f} contracts x ${cur_price:.4f} = ${payout:.2f}  (PnL ${pnl:+.2f})")

                if relay_client is None:
                    print(f"    -> Skipping redemption (no relay client). Set builder credentials.")
                    continue

                # Submit on-chain redemption via Polymarket relayer
                live_pos_data = live_by_token[token_id]
                try:
                    redeemed = await loop.run_in_executor(
                        None, _redeem_position_sync, relay_client, live_pos_data
                    )
                except Exception as exc:
                    print(f"    -> Redemption error: {exc}")
                    redeemed = False

                if not redeemed:
                    print(f"    -> Redemption failed, will retry next cycle")
                    continue

                print(f"    -> Redeemed. USDC returned to wallet.")

                # Update state after confirmed redemption
                if "closed_positions" not in state:
                    state["closed_positions"] = []
                state["closed_positions"].append({
                    "token_id":        token_id,
                    "market_question": pos.get("market_question", ""),
                    "outcome":         outcome,
                    "strategy":        pos.get("strategy", "unknown"),
                    "contracts":       contracts,
                    "avg_cost":        avg_cost,
                    "resolved_price":  cur_price,
                    "payout":          round(payout, 4),
                    "realized_pnl":    round(pnl, 4),
                    "opened_at":       pos.get("opened_at", now_ts),
                    "closed_at":       now_ts,
                    "note":            "auto_redeemed_win",
                })
                del state["positions"][token_id]
                state["usdc_balance"] = round(state.get("usdc_balance", 0) + payout, 4)
                state["realized_pnl"] = round(state.get("realized_pnl", 0) + pnl, 4)
                wins += 1
                dirty = True
                continue

            # ── Still live on-chain: nothing to do ────────────────────────
            if token_id in live_token_ids:
                continue

            # ── Not in live positions: market has resolved ─────────────────
            # Try to determine outcome from Gamma API (falls back to token_id lookup)
            mkt = await fetch_market_outcome(condition_id, client, token_id=token_id)

            if mkt is None:
                # Can't verify — skip this one; try again next cycle
                print(f"  [SKIP] Cannot fetch market info for: {question}")
                skipped += 1
                continue

            if not mkt.get("resolved", False):
                # Market not yet resolved according to Gamma (might be delayed)
                print(f"  [PENDING] Not yet resolved per Gamma: {question}")
                skipped += 1
                continue

            # resolved_price: 1.0 = YES won, 0.0 = NO won
            resolved_price = float(mkt.get("resolvedPrice") or 0.0)

            # Did our position win?
            if outcome == "YES":
                won          = resolved_price >= 0.99
                payout_price = resolved_price
            else:  # NO position
                won          = resolved_price <= 0.01
                payout_price = 1.0 - resolved_price

            payout       = contracts * payout_price
            realized_pnl = round(payout - cost_basis, 4)

            # ── Update state ──────────────────────────────────────────────
            if "closed_positions" not in state:
                state["closed_positions"] = []

            state["closed_positions"].append({
                "token_id":       token_id,
                "market_question": pos.get("market_question", ""),
                "outcome":        outcome,
                "strategy":       pos.get("strategy", "unknown"),
                "contracts":      contracts,
                "avg_cost":       avg_cost,
                "resolved_price": resolved_price,
                "payout":         round(payout, 4),
                "realized_pnl":   realized_pnl,
                "opened_at":      pos.get("opened_at", now_ts),
                "closed_at":      now_ts,
                "note":           "auto_settled_win" if won else "auto_settled_loss",
            })

            del state["positions"][token_id]

            # Return USDC to balance if we won
            if won:
                state["usdc_balance"]  = round(state.get("usdc_balance", 0) + payout, 4)
                state["realized_pnl"]  = round(state.get("realized_pnl", 0) + realized_pnl, 4)
                print(f"  [WIN]  {question}")
                print(f"    +${payout:.2f} returned  (PnL ${realized_pnl:+.2f})")
                wins += 1
            else:
                state["realized_pnl"]  = round(state.get("realized_pnl", 0) + realized_pnl, 4)
                print(f"  [LOSS] {question}")
                print(f"    Expired worthless  (PnL ${realized_pnl:+.2f})")
                losses += 1

            dirty = True

        # ── Save if anything changed ──────────────────────────────────────
        if dirty:
            state["open_positions"] = len(state.get("positions", {}))
            save_state(state)
            print(f"\n  Saved. Closed: {wins} wins + {losses} losses. Skipped: {skipped}.")
        else:
            print(f"  Nothing to close. Skipped: {skipped}.")
# ─────────────────────────────────────────────────────────────────────────────


# ── Main loop ─────────────────────────────────────────────────────────────────
async def main() -> None:
    addr_display = f"{FUNDER_ADDR[:6]}...{FUNDER_ADDR[-4:]}" if len(FUNDER_ADDR) > 10 else FUNDER_ADDR or "NOT SET"
    print(f"settle_positions started | wallet={addr_display} | state={STATE_FILE}")
    print(f"Schedule: every {INTERVAL//60}m at :01, :06, :11, :16 ... (UTC)\n")

    if not FUNDER_ADDR:
        print("[ERROR] POLYMARKET_FUNDER_ADDRESS not set. Exiting.")
        return

    while True:
        wait = seconds_until_next_run()
        if wait > 0:
            next_utc = datetime.now(timezone.utc)
            print(f"[SLEEP] {wait:.0f}s until next run", flush=True)
            await asyncio.sleep(wait)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n{'─'*56}")
        print(f"[RUN] {ts}")
        print(f"{'─'*56}")

        try:
            await run_settlement()
        except Exception as exc:
            print(f"  [ERROR] {exc}")

        # Brief sleep to avoid double-firing at the exact second boundary
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
