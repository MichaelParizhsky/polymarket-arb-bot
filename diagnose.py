#!/usr/bin/env python3
"""
Polybot Diagnostic Script
=========================
Fetches live Polymarket data and walks through every filter in quick_resolution
and resolution strategies — showing EXACTLY why each market is accepted or rejected.

No trades placed. Read-only.

Usage:
    python diagnose.py
    python diagnose.py --hours 48      # scan up to 48h window
    python diagnose.py --top 50        # show top 50 closest markets
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
import time
import os

import httpx

# ── Load .env manually (no dotenv dependency needed) ──────────────────────────
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

# ── Mirrors the strategy thresholds ──────────────────────────────────────────
QR_BASE_CONVICTION = float(os.getenv("QUICK_RESOLUTION_MIN_CONVICTION", "0.78"))
QR_MIN_VOLUME      = float(os.getenv("QUICK_RESOLUTION_MIN_VOLUME", "100.0"))
RES_MIN_VOLUME     = 500.0
RES_MAX_HOURS      = 48.0

CATEGORY_EDGE_MUL = {"sports": 1.5, "crypto": 1.2}

RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"


def _hours_left(end_date_iso: str) -> float | None:
    if not end_date_iso:
        return None
    try:
        s = end_date_iso.strip().rstrip("Z")
        if "T" not in s:
            # Date-only like "2026-03-31" — treat as UTC midnight
            s += "T00:00:00"
        end_dt = datetime.datetime.fromisoformat(s).replace(
            tzinfo=datetime.timezone.utc
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        return (end_dt - now).total_seconds() / 3600
    except Exception:
        return None


def _tiered_conviction(hours_left: float) -> tuple[float, float]:
    b = QR_BASE_CONVICTION
    if hours_left <= 0.5:
        return (b, 0.006)
    elif hours_left <= 2.0:
        return (max(b - 0.06, 0.75), 0.010)
    elif hours_left <= 6.0:
        return (max(b - 0.10, 0.72), 0.015)
    elif hours_left <= 12.0:
        return (max(b - 0.14, 0.70), 0.020)
    else:
        return (max(b - 0.18, 0.68), 0.025)


def _required_edge(hours_left: float) -> float:
    if hours_left <= 1.0:  return 0.015
    if hours_left <= 4.0:  return 0.020
    if hours_left <= 12.0: return 0.025
    return 0.030


def _calc_fee(price: float, market_type: str = "standard") -> float:
    if market_type == "crypto_5m":
        return 0.25 * (price * (1 - price)) ** 2
    elif market_type in ("sports", "dcm"):
        return 0.003
    return 0.002


SPORTS_SERIES_IDS = {
    "nba": 10345, "nhl": 10346, "mls": 10189, "nfl": 10187,
    "bundesliga": 10194, "cfb": 10210, "cbb": 10470,
    "norway": 10362, "brazil": 10359, "japan": 10360,
}


async def fetch_markets(client: httpx.AsyncClient, hours: float) -> list[dict]:
    """Fetch markets sorted by soonest expiry — includes sports via events API."""
    now = datetime.datetime.now(datetime.timezone.utc)
    end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (now + datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out: list[dict] = []
    seen: set[str] = set()

    def _add(m: dict, category: str = "") -> None:
        cid = m.get("conditionId") or m.get("condition_id") or m.get("id", "")
        if cid in seen:
            return
        end_iso = m.get("endDate") or m.get("endDateIso", "")
        hl = _hours_left(end_iso)
        if hl is None or hl <= 0 or hl > hours:
            return
        m["_hours_left"] = hl
        m["_volume"] = float(m.get("volumeNum") or m.get("volume") or 0)
        m["_category"] = category or (m.get("category") or "").lower()
        out.append(m)
        seen.add(cid)

    # 1. Standard /markets with date filter
    try:
        resp = await client.get(
            f"{GAMMA_BASE}/markets",
            params={"active": "true", "closed": "false", "limit": "500",
                    "end_date_min": end_min, "end_date_max": end_max},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
        for m in (raw if isinstance(raw, list) else raw.get("data", [])):
            _add(m)
    except Exception as e:
        print(f"{RED}  /markets fetch error: {e}{RESET}")

    # 2. Sports markets via events API
    for sport, sid in SPORTS_SERIES_IDS.items():
        try:
            resp = await client.get(
                f"{GAMMA_BASE}/events",
                params={"series_id": str(sid), "active": "true", "limit": "200",
                        "end_date_min": end_min, "end_date_max": end_max},
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            events = data if isinstance(data, list) else data.get("data", [])
            for event in events:
                for m in event.get("markets", []):
                    _add(m, category=sport)
        except Exception:
            pass

    out.sort(key=lambda m: m["_hours_left"])
    return out


async def fetch_orderbook(client: httpx.AsyncClient, token_id: str) -> dict | None:
    try:
        resp = await client.get(
            f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=8
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def _best_ask(ob: dict | None, side: str = "YES") -> float | None:
    if not ob:
        return None
    asks = ob.get("asks", [])
    if not asks:
        return None
    try:
        prices = sorted(float(a["price"]) for a in asks if float(a["price"]) > 0)
        return prices[0] if prices else None
    except Exception:
        return None


def _mid(ob: dict | None) -> float | None:
    if not ob:
        return None
    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    try:
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        if best_bid and best_ask:
            return (best_bid + best_ask) / 2
    except Exception:
        pass
    return None


async def diagnose(hours: float = 24.0, top: int = 40, verbose: bool = False):
    print(f"\n{BOLD}{CYAN}=== Polybot Diagnostic ==={RESET}")
    print(f"Fetching markets expiring within {hours}h...\n")

    qr_signals   = []
    res_signals  = []
    skip_reasons = {}

    def _skip(reason: str):
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    async with httpx.AsyncClient() as client:
        markets = await fetch_markets(client, hours)

        print(f"Found {BOLD}{len(markets)}{RESET} markets in window.\n")

        if not markets:
            print(f"{RED}No markets found. Check API connectivity or widen --hours window.{RESET}")
            return

        print(f"{'Market':<55} {'Hrs':>5} {'Vol':>8} {'Mid':>6}  QR?  Res?  Notes")
        print("-" * 110)

        for m in markets[:top]:
            hl   = m["_hours_left"]
            vol  = m["_volume"]
            cat  = m["_category"]
            q    = m.get("question", "")[:52]
            tokens_raw = m.get("clobTokenIds") or []
            if isinstance(tokens_raw, str):
                try: tokens_raw = json.loads(tokens_raw)
                except: tokens_raw = []
            outcomes = m.get("outcomes") or ["Yes", "No"]
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: outcomes = ["Yes", "No"]

            # Use first token as the "YES" side (works for binary and sports markets)
            yes_tid = next(
                (str(tid) for tid, out in zip(tokens_raw, outcomes) if str(out).lower() == "yes"),
                str(tokens_raw[0]) if tokens_raw else None
            )

            # Fetch orderbook for YES/first token
            yes_ob = None
            mid_val = None
            best_ask_val = None
            if yes_tid:
                yes_ob = await fetch_orderbook(client, yes_tid)
                mid_val = _mid(yes_ob)
                best_ask_val = _best_ask(yes_ob)

            mid_str = f"{mid_val:.3f}" if mid_val else "  N/A"
            vol_str = f"${vol:,.0f}"

            # ── Quick Resolution check ────────────────────────────────────────────
            qr_ok   = False
            qr_note = []

            if vol < QR_MIN_VOLUME:
                qr_note.append(f"vol<{QR_MIN_VOLUME:.0f}")
                _skip("QR:low_volume")
            elif mid_val is None:
                qr_note.append("no_ob")
                _skip("QR:no_orderbook")
            else:
                conv_thresh, min_edge = _tiered_conviction(hl)
                mtype = "crypto_5m" if any(k in q.lower() for k in ["5-min","15-min","5m","15m","hourly"]) \
                        else "sports" if any(k in q.lower() for k in ["nba","nfl","nhl","mlb","ufc","win","game","match"]) \
                        else "standard"
                fee = _calc_fee(mid_val, mtype)

                if mid_val >= conv_thresh:
                    net_edge = (1.0 - best_ask_val) - fee if best_ask_val else None
                    if net_edge is None:
                        qr_note.append("no_ask")
                        _skip("QR:no_ask")
                    elif net_edge >= min_edge:
                        qr_ok = True
                        qr_signals.append({"q": q, "hl": hl, "mid": mid_val, "edge": net_edge, "side": "YES"})
                        qr_note.append(f"YES edge={net_edge:.3f}")
                    else:
                        qr_note.append(f"YES edge={net_edge:.3f}<{min_edge:.3f}")
                        _skip("QR:edge_too_low")
                elif mid_val <= (1 - conv_thresh):
                    net_edge = mid_val - fee if mid_val else None
                    if net_edge and net_edge >= min_edge:
                        qr_ok = True
                        qr_signals.append({"q": q, "hl": hl, "mid": mid_val, "edge": net_edge, "side": "NO"})
                        qr_note.append(f"NO edge={net_edge:.3f}")
                    else:
                        qr_note.append(f"mid={mid_val:.3f} below conv={conv_thresh:.2f}")
                        _skip("QR:low_conviction")
                else:
                    qr_note.append(f"mid={mid_val:.3f} conv_need={conv_thresh:.2f}")
                    _skip("QR:low_conviction")

            # ── Resolution check ─────────────────────────────────────────────────
            res_ok   = False
            res_note = []

            if vol < RES_MIN_VOLUME:
                res_note.append(f"vol<{RES_MIN_VOLUME:.0f}")
                _skip("Res:low_volume")
            elif mid_val is None:
                res_note.append("no_ob")
                _skip("Res:no_orderbook")
            else:
                req_edge = _required_edge(hl) * CATEGORY_EDGE_MUL.get(cat, 1.0)
                # Tier 1: endgame (97%+, <4h)
                if hl <= 4.0 and 0.970 <= mid_val <= 0.999:
                    net_edge = (1.0 - best_ask_val) - 0.002 if best_ask_val else None
                    if net_edge and net_edge >= 0.002:
                        res_ok = True
                        res_signals.append({"q": q, "hl": hl, "mid": mid_val, "edge": net_edge, "tier": 1})
                        res_note.append(f"T1 YES edge={net_edge:.3f}")
                    else:
                        res_note.append("T1 ask_too_high or no_ask")
                elif hl <= 4.0 and 0.001 <= mid_val <= 0.030:
                    res_ok = True
                    res_note.append("T1 NO")
                # Tier 2: likely (90-97%, <12h) — cascade from tier 1
                if not res_ok and hl <= 12.0 and 0.90 <= mid_val <= 0.970:
                    net_edge = (1.0 - best_ask_val) - 0.002 if best_ask_val else None
                    if net_edge and net_edge >= req_edge:
                        res_ok = True
                        res_signals.append({"q": q, "hl": hl, "mid": mid_val, "edge": net_edge, "tier": 2})
                        res_note.append(f"T2 YES edge={net_edge:.3f}")
                    else:
                        edge_str = f"{net_edge:.3f}" if net_edge else "N/A"
                        res_note.append(f"T2 edge={edge_str}<{req_edge:.3f}")
                        _skip("Res:edge_too_low")
                elif not res_ok and hl <= 12.0 and 0.030 <= mid_val <= 0.10:
                    res_note.append(f"T2 NO mid={mid_val:.3f} (low prob)")
                    _skip("Res:low_conviction")
                elif not res_ok:
                    res_note.append(f"mid={mid_val:.3f} not in any tier")
                    _skip("Res:mid_out_of_range")

            qr_col  = f"{GREEN}+{RESET}" if qr_ok  else f"{RED}x{RESET}"
            res_col = f"{GREEN}+{RESET}" if res_ok else f"{RED}x{RESET}"

            notes = []
            if qr_note:  notes.append(f"QR:{','.join(qr_note)}")
            if res_note: notes.append(f"R:{','.join(res_note)}")
            notes_str = " | ".join(notes)[:50]

            print(
                f"{q:<55} {hl:>5.1f} {vol_str:>8} {mid_str:>6}  "
                f" {qr_col}    {res_col}  {DIM}{notes_str}{RESET}"
            )

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "-" * 110)
    print(f"\n{BOLD}SIGNALS FOUND:{RESET}")
    print(f"  QuickResolution : {GREEN}{len(qr_signals)}{RESET}")
    for s in qr_signals:
        print(f"    -> {s['side']} | {s['q'][:60]} | {s['hl']:.1f}h | mid={s['mid']:.3f} | edge={s['edge']:.3f}")
    print(f"  Resolution      : {GREEN}{len(res_signals)}{RESET}")
    for s in res_signals:
        print(f"    -> Tier{s['tier']} | {s['q'][:60]} | {s['hl']:.1f}h | mid={s['mid']:.3f} | edge={s['edge']:.3f}")

    print(f"\n{BOLD}SKIP REASON BREAKDOWN:{RESET}")
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        bar = "#" * min(count, 40)
        print(f"  {reason:<30} {count:>4}  {YELLOW}{bar}{RESET}")

    if not qr_signals and not res_signals:
        print(f"\n{RED}{BOLD}NO SIGNALS -- Likely causes:{RESET}")
        top_reason = max(skip_reasons, key=skip_reasons.get) if skip_reasons else None
        if top_reason in ("QR:low_conviction", "Res:mid_out_of_range"):
            print("  * Markets not at price extremes. Most are trading near 50%.")
            print("    These are contested markets -- no clear winner yet.")
        if "low_volume" in str(top_reason):
            print("  * Volume filter blocking markets.")
            print(f"    Try: python diagnose.py --hours {hours} (and check if vol field is populated)")
        if "no_orderbook" in str(top_reason):
            print("  * Orderbook fetch failing. Check API connectivity / rate limits.")
        print("\n  Run: python diagnose.py --hours 72  to widen the window")
        print("  Run: python diagnose.py --hours 168  to see 1-week window\n")
    else:
        print(f"\n{GREEN}{BOLD}Bot SHOULD be generating signals. If it's not:{RESET}")
        print("  1. Check Railway deployment has latest code (git pull on Railway)")
        print("  2. Check Railway logs for import errors or crashes")
        print("  3. Check MIN_TRADE_INTERVAL / TOKEN_COOLDOWN env vars")
        print("  4. Check PAPER_TRADING=true is set (signals execute in paper mode)")


def main():
    parser = argparse.ArgumentParser(description="Polybot diagnostic scanner")
    parser.add_argument("--hours", type=float, default=24.0, help="Hours window to scan (default 24)")
    parser.add_argument("--top",   type=int,   default=40,   help="Max markets to display (default 40)")
    parser.add_argument("--verbose", action="store_true",    help="Show extra detail")
    args = parser.parse_args()

    asyncio.run(diagnose(hours=args.hours, top=args.top, verbose=args.verbose))


if __name__ == "__main__":
    main()
