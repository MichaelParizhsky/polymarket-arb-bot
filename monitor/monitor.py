#!/usr/bin/env python3
"""
Polymarket Bot Monitor
======================
Polls one or more bot dashboards every N minutes, detects anomalies,
calls Claude Haiku for terse analysis, and sends Telegram alerts.

Required env vars:
  DASHBOARD_URLS        - comma-separated list of Name=URL pairs
                          e.g. "Bot A=https://bot-a.railway.app,Bot B=https://bot-b.railway.app"
                          Single bot: just a URL works too.
  DASHBOARD_API_KEY     - shared API key for all dashboards
  TELEGRAM_BOT_TOKEN    - from @BotFather on Telegram
  TELEGRAM_CHAT_ID      - your chat ID
  ANTHROPIC_API_KEY     - for Claude Haiku analysis (optional but recommended)

Optional:
  POLL_INTERVAL_SECONDS  - default 300 (5 min)
  DRAWDOWN_ALERT_PCT     - default 8.0 (alert at 8% drawdown)
  NO_TRADES_ALERT_HOURS  - default 2.0
  ALERT_COOLDOWN_MINUTES - default 30 (suppress duplicate alerts within window)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

# Support DASHBOARD_URLS (multi) or DASHBOARD_URL (single, legacy)
_raw_urls = os.getenv("DASHBOARD_URLS") or os.getenv("DASHBOARD_URL", "")
DASHBOARD_API_KEY     = os.getenv("DASHBOARD_API_KEY", "")
TELEGRAM_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY", "")

POLL_INTERVAL         = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
DRAWDOWN_ALERT_PCT    = float(os.getenv("DRAWDOWN_ALERT_PCT", "8.0"))
NO_TRADES_ALERT_HOURS = float(os.getenv("NO_TRADES_ALERT_HOURS", "2.0"))
ALERT_COOLDOWN        = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30")) * 60
DAILY_INTERVAL        = 86400


def _parse_urls(raw: str) -> list[tuple[str, str]]:
    """Parse 'Bot A=https://...,Bot B=https://...' into [(name, url), ...]."""
    results = []
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            name, url = entry.split("=", 1)
            results.append((name.strip(), url.strip().rstrip("/")))
        else:
            results.append((f"Bot {chr(65 + i)}", entry.rstrip("/")))
    return results

BOTS: list[tuple[str, str]] = _parse_urls(_raw_urls)


# ── Snapshot ──────────────────────────────────────────────────────────────────

@dataclass
class BotSnapshot:
    name: str
    url: str
    reachable: bool
    status: dict = field(default_factory=dict)
    system: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def pnl(self) -> float:
        return self.status.get("pnl", 0.0)

    @property
    def pnl_pct(self) -> float:
        return self.status.get("pnl_pct", 0.0)

    @property
    def drawdown_pct(self) -> float:
        return self.system.get("risk", {}).get("drawdown_pct", 0.0)

    @property
    def hard_stop(self) -> bool:
        return (
            self.system.get("risk", {}).get("hard_stop", False)
            or self.system.get("risk", {}).get("health_grade") == "CRITICAL"
        )

    @property
    def health_grade(self) -> str:
        return self.system.get("risk", {}).get("health_grade", "N/A")

    @property
    def total_trades(self) -> int:
        return self.status.get("total_trades", 0)

    @property
    def balance(self) -> float:
        return self.status.get("balance", 0.0)

    @property
    def paper_trading(self) -> bool:
        return self.status.get("paper_trading", True)

    @property
    def uptime(self) -> str:
        return self.status.get("uptime", "?")

    @property
    def mode(self) -> str:
        return "PAPER" if self.paper_trading else "LIVE"

    @property
    def label(self) -> str:
        return f"{self.name} · {self.mode}"

    def to_haiku_context(self, alert_type: str) -> str:
        return json.dumps({
            "bot": self.name,
            "alert_type": alert_type,
            "reachable": self.reachable,
            "pnl_usdc": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 3),
            "drawdown_pct": round(self.drawdown_pct, 2),
            "hard_stop": self.hard_stop,
            "health_grade": self.health_grade,
            "total_trades": self.total_trades,
            "balance_usdc": round(self.balance, 2),
            "paper_trading": self.paper_trading,
            "uptime": self.uptime,
            "risk_flags": self.system.get("risk", {}).get("flags", []),
            "active_strategies": [
                k for k, v in self.system.get("strategies", {}).items()
                if v.get("enabled")
            ],
        }, indent=2)


# ── Per-bot watcher state ──────────────────────────────────────────────────────

@dataclass
class BotWatcher:
    name: str
    url: str
    _last_alert: dict = field(default_factory=dict)
    _last_trade_count: int = -1
    _last_trade_count_ts: float = field(default_factory=time.time)
    _last_daily_summary: float = 0.0

    def cooldown_ok(self, key: str) -> bool:
        return time.time() - self._last_alert.get(key, 0) > ALERT_COOLDOWN

    def mark_alerted(self, key: str) -> None:
        self._last_alert[key] = time.time()

    def clear_alert(self, key: str) -> None:
        self._last_alert.pop(key, None)

    async def fetch(self) -> BotSnapshot:
        headers = {"X-Api-Key": DASHBOARD_API_KEY} if DASHBOARD_API_KEY else {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                status_r, system_r = await asyncio.gather(
                    client.get(f"{self.url}/api/status", headers=headers),
                    client.get(f"{self.url}/api/system", headers=headers),
                    return_exceptions=True,
                )
            return BotSnapshot(
                name=self.name,
                url=self.url,
                reachable=True,
                status=status_r.json() if not isinstance(status_r, Exception) and status_r.status_code == 200 else {},
                system=system_r.json() if not isinstance(system_r, Exception) and system_r.status_code == 200 else {},
            )
        except Exception:
            return BotSnapshot(name=self.name, url=self.url, reachable=False)


# ── Monitor ───────────────────────────────────────────────────────────────────

class Monitor:
    def __init__(self, watchers: list[BotWatcher]) -> None:
        self.watchers = watchers

    async def send_telegram(self, text: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print(f"[ALERT — no Telegram configured]\n{text}\n")
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                )
                if resp.status_code != 200:
                    print(f"Telegram error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"Telegram send failed: {e}")

    async def haiku_analyze(self, snap: BotSnapshot, alert_type: str) -> str:
        if not ANTHROPIC_API_KEY:
            return ""
        try:
            import anthropic as _anthropic
            client = _anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            msg = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Polymarket trading bot alert: {alert_type}\n\n"
                        f"Snapshot:\n{snap.to_haiku_context(alert_type)}\n\n"
                        "In 2 short sentences: what happened and one concrete action to take. "
                        "Be direct. No preamble."
                    ),
                }],
            )
            return msg.content[0].text.strip()
        except Exception as e:
            return f"_(analysis unavailable: {e})_"

    async def check_watcher(self, w: BotWatcher) -> None:
        snap = await w.fetch()

        # 1. Service unreachable
        if not snap.reachable:
            if w.cooldown_ok("unreachable"):
                analysis = await self.haiku_analyze(snap, "Service unreachable")
                await self.send_telegram(
                    f"🔴 *Unreachable* `[{snap.label}]`\n"
                    f"Dashboard not responding at `{snap.url}`\n\n{analysis}"
                )
                w.mark_alerted("unreachable")
            return
        w.clear_alert("unreachable")

        # 2. Hard stop
        if snap.hard_stop:
            if w.cooldown_ok("hard_stop"):
                analysis = await self.haiku_analyze(snap, "Hard stop active")
                await self.send_telegram(
                    f"🛑 *Hard Stop* `[{snap.label}]`\n"
                    f"Drawdown: *{snap.drawdown_pct:.1f}%* | Balance: *${snap.balance:,.2f}*\n\n{analysis}"
                )
                w.mark_alerted("hard_stop")

        # 3. Drawdown threshold
        if snap.drawdown_pct >= DRAWDOWN_ALERT_PCT:
            key = f"drawdown_{int(snap.drawdown_pct)}"
            if w.cooldown_ok(key):
                analysis = await self.haiku_analyze(snap, f"Drawdown {snap.drawdown_pct:.1f}%")
                await self.send_telegram(
                    f"⚠️ *Drawdown Alert* `[{snap.label}]`\n"
                    f"Drawdown: *{snap.drawdown_pct:.1f}%* | PnL: *${snap.pnl:+.2f}* ({snap.pnl_pct:+.2f}%)\n\n{analysis}"
                )
                w.mark_alerted(key)

        # 4. No new trades — stalled
        current = snap.total_trades
        if w._last_trade_count == -1:
            w._last_trade_count = current
            w._last_trade_count_ts = time.time()
        elif current > w._last_trade_count:
            w._last_trade_count = current
            w._last_trade_count_ts = time.time()
        else:
            stalled_h = (time.time() - w._last_trade_count_ts) / 3600
            if stalled_h >= NO_TRADES_ALERT_HOURS and w.cooldown_ok("stalled"):
                analysis = await self.haiku_analyze(snap, f"No trades in {stalled_h:.1f}h")
                await self.send_telegram(
                    f"⚠️ *Stalled* `[{snap.label}]`\n"
                    f"No new trades in *{stalled_h:.1f}h*. Total: {current}\n\n{analysis}"
                )
                w.mark_alerted("stalled")

        # 5. CRITICAL health (not already covered by hard stop)
        if snap.health_grade == "CRITICAL" and not snap.hard_stop and w.cooldown_ok("critical_grade"):
            flags = snap.system.get("risk", {}).get("flags", [])
            flags_text = "\n".join(f"• {f}" for f in flags) if flags else "_No details_"
            await self.send_telegram(
                f"🔴 *Health: CRITICAL* `[{snap.label}]`\n{flags_text}"
            )
            w.mark_alerted("critical_grade")

        # Daily summary
        if time.time() - w._last_daily_summary >= DAILY_INTERVAL:
            await self.send_daily_summary(snap)
            w._last_daily_summary = time.time()

    async def send_daily_summary(self, snap: BotSnapshot) -> None:
        if not snap.reachable:
            await self.send_telegram(f"📊 *Daily Summary* `[{snap.label}]` — unreachable.")
            return
        strategies_on = [k for k, v in snap.system.get("strategies", {}).items() if v.get("enabled")]
        flags = snap.system.get("risk", {}).get("flags", [])
        flags_text = ("\n" + "\n".join(f"• {f}" for f in flags)) if flags else " None"
        await self.send_telegram(
            f"📊 *Daily Summary* `[{snap.label}]`\n"
            f"PnL: *${snap.pnl:+.2f}* ({snap.pnl_pct:+.2f}%)\n"
            f"Balance: ${snap.balance:,.2f}\n"
            f"Drawdown: {snap.drawdown_pct:.1f}%\n"
            f"Health: *{snap.health_grade}*\n"
            f"Trades: {snap.total_trades}\n"
            f"Uptime: {snap.uptime}\n"
            f"Active: {', '.join(strategies_on) or 'none'}\n"
            f"Flags:{flags_text}"
        )

    async def run(self) -> None:
        names = ", ".join(f"{n} ({u})" for n, u in BOTS)
        print(f"Monitor started. Watching: {names} — polling every {POLL_INTERVAL}s")
        bot_list = "\n".join(f"• {n}" for n, _ in BOTS)
        await self.send_telegram(
            f"🟢 *Monitor Online*\nWatching {len(BOTS)} bot(s):\n{bot_list}\n\n"
            f"Poll: every {POLL_INTERVAL // 60} min | "
            f"Drawdown alert: {DRAWDOWN_ALERT_PCT}% | "
            f"Stall alert: {NO_TRADES_ALERT_HOURS}h"
        )
        while True:
            try:
                await asyncio.gather(*[self.check_watcher(w) for w in self.watchers])
            except Exception as e:
                print(f"Monitor loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _raw_urls:
        print("ERROR: Set DASHBOARD_URLS (e.g. 'Bot A=https://bot-a.railway.app,Bot B=https://bot-b.railway.app')")
        raise SystemExit(1)
    missing = [v for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(v)]
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}")
        raise SystemExit(1)
    watchers = [BotWatcher(name=name, url=url) for name, url in BOTS]
    asyncio.run(Monitor(watchers).run())
