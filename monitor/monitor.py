#!/usr/bin/env python3
"""
Polymarket Bot Monitor
======================
Polls the bot dashboard every N minutes, detects anomalies,
calls Claude Haiku for terse analysis, and sends Telegram alerts.

Required env vars:
  DASHBOARD_URL         - e.g. https://polymarket-bot.up.railway.app
  DASHBOARD_API_KEY     - same key set on the bot dashboard
  TELEGRAM_BOT_TOKEN    - from @BotFather on Telegram
  TELEGRAM_CHAT_ID      - your chat ID (run get_chat_id.py to find it)
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

DASHBOARD_URL       = os.getenv("DASHBOARD_URL", "").rstrip("/")
DASHBOARD_API_KEY   = os.getenv("DASHBOARD_API_KEY", "")
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")

POLL_INTERVAL         = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
DRAWDOWN_ALERT_PCT    = float(os.getenv("DRAWDOWN_ALERT_PCT", "8.0"))
NO_TRADES_ALERT_HOURS = float(os.getenv("NO_TRADES_ALERT_HOURS", "2.0"))
ALERT_COOLDOWN        = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30")) * 60
DAILY_INTERVAL        = 86400  # 24h between summary messages


# ── Snapshot ──────────────────────────────────────────────────────────────────

@dataclass
class BotSnapshot:
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
        return self.system.get("risk", {}).get("hard_stop", False) or \
               self.system.get("risk", {}).get("health_grade") == "CRITICAL"

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

    def to_haiku_context(self, alert_type: str) -> str:
        return json.dumps({
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


# ── Monitor ───────────────────────────────────────────────────────────────────

class Monitor:
    def __init__(self) -> None:
        self._last_alert: dict[str, float] = {}
        self._last_trade_count: int = -1
        self._last_trade_count_ts: float = time.time()
        self._last_daily_summary: float = 0.0

    # ── API calls ────────────────────────────────────────────────────────────

    async def fetch_snapshot(self) -> BotSnapshot:
        headers = {"X-Api-Key": DASHBOARD_API_KEY} if DASHBOARD_API_KEY else {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                status_r, system_r = await asyncio.gather(
                    client.get(f"{DASHBOARD_URL}/api/status", headers=headers),
                    client.get(f"{DASHBOARD_URL}/api/system", headers=headers),
                    return_exceptions=True,
                )
                return BotSnapshot(
                    reachable=True,
                    status=status_r.json() if not isinstance(status_r, Exception) and status_r.status_code == 200 else {},
                    system=system_r.json() if not isinstance(system_r, Exception) and system_r.status_code == 200 else {},
                )
        except Exception:
            return BotSnapshot(reachable=False)

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

    async def send_telegram(self, text: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print(f"[ALERT — no Telegram configured]\n{text}\n")
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": text,
                        "parse_mode": "Markdown",
                    },
                )
                if resp.status_code != 200:
                    print(f"Telegram error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"Telegram send failed: {e}")

    # ── Alert helpers ────────────────────────────────────────────────────────

    def _cooldown_ok(self, key: str) -> bool:
        return time.time() - self._last_alert.get(key, 0) > ALERT_COOLDOWN

    def _mark_alerted(self, key: str) -> None:
        self._last_alert[key] = time.time()

    # ── Checks ───────────────────────────────────────────────────────────────

    async def check_and_alert(self, snap: BotSnapshot) -> None:
        # 1. Service unreachable
        if not snap.reachable:
            if self._cooldown_ok("unreachable"):
                analysis = await self.haiku_analyze(snap, "Service unreachable — dashboard not responding")
                await self.send_telegram(
                    f"🔴 *Bot Unreachable* `[{snap.mode}]`\n"
                    f"Dashboard is not responding at `{DASHBOARD_URL}`\n\n"
                    f"{analysis}"
                )
                self._mark_alerted("unreachable")
            return  # no point checking further

        # clear unreachable alert if we recovered
        if "unreachable" in self._last_alert:
            del self._last_alert["unreachable"]

        # 2. Hard stop / permanent lock
        if snap.hard_stop:
            if self._cooldown_ok("hard_stop"):
                analysis = await self.haiku_analyze(snap, "Hard stop or permanent lock active")
                await self.send_telegram(
                    f"🛑 *Hard Stop Active* `[{snap.mode}]`\n"
                    f"Drawdown: *{snap.drawdown_pct:.1f}%* | Balance: *${snap.balance:,.2f}*\n\n"
                    f"{analysis}"
                )
                self._mark_alerted("hard_stop")

        # 3. Drawdown threshold
        if snap.drawdown_pct >= DRAWDOWN_ALERT_PCT:
            key = f"drawdown_{int(snap.drawdown_pct)}"
            if self._cooldown_ok(key):
                analysis = await self.haiku_analyze(snap, f"Drawdown at {snap.drawdown_pct:.1f}%")
                await self.send_telegram(
                    f"⚠️ *Drawdown Alert* `[{snap.mode}]`\n"
                    f"Drawdown: *{snap.drawdown_pct:.1f}%* | "
                    f"PnL: *${snap.pnl:+.2f}* ({snap.pnl_pct:+.2f}%)\n\n"
                    f"{analysis}"
                )
                self._mark_alerted(key)

        # 4. No new trades — bot may be stalled
        current = snap.total_trades
        if self._last_trade_count == -1:
            self._last_trade_count = current
            self._last_trade_count_ts = time.time()
        elif current > self._last_trade_count:
            self._last_trade_count = current
            self._last_trade_count_ts = time.time()
        else:
            stalled_h = (time.time() - self._last_trade_count_ts) / 3600
            if stalled_h >= NO_TRADES_ALERT_HOURS and self._cooldown_ok("stalled"):
                analysis = await self.haiku_analyze(snap, f"No new trades in {stalled_h:.1f}h")
                await self.send_telegram(
                    f"⚠️ *Bot May Be Stalled* `[{snap.mode}]`\n"
                    f"No new trades in *{stalled_h:.1f}h*. Total: {current}\n\n"
                    f"{analysis}"
                )
                self._mark_alerted("stalled")

        # 5. Health grade CRITICAL (and not already in hard_stop alert)
        if snap.health_grade == "CRITICAL" and not snap.hard_stop and self._cooldown_ok("critical_grade"):
            flags = snap.system.get("risk", {}).get("flags", [])
            flags_text = "\n".join(f"• {f}" for f in flags) if flags else "_No details_"
            await self.send_telegram(
                f"🔴 *Portfolio Health: CRITICAL* `[{snap.mode}]`\n{flags_text}"
            )
            self._mark_alerted("critical_grade")

    async def send_daily_summary(self, snap: BotSnapshot) -> None:
        if not snap.reachable:
            await self.send_telegram("📊 *Daily Summary* — bot unreachable, no data available.")
            return

        strategies_on = [
            k for k, v in snap.system.get("strategies", {}).items()
            if v.get("enabled")
        ]
        flags = snap.system.get("risk", {}).get("flags", [])
        flags_text = ("\n" + "\n".join(f"• {f}" for f in flags)) if flags else " None"

        await self.send_telegram(
            f"📊 *Daily Summary* `[{snap.mode}]`\n"
            f"PnL: *${snap.pnl:+.2f}* ({snap.pnl_pct:+.2f}%)\n"
            f"Balance: ${snap.balance:,.2f}\n"
            f"Drawdown: {snap.drawdown_pct:.1f}%\n"
            f"Health: *{snap.health_grade}*\n"
            f"Trades: {snap.total_trades}\n"
            f"Uptime: {snap.uptime}\n"
            f"Active: {', '.join(strategies_on) or 'none'}\n"
            f"Flags:{flags_text}"
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        print(f"Monitor started. Polling {DASHBOARD_URL} every {POLL_INTERVAL}s")
        await self.send_telegram(
            f"🟢 *Monitor Online*\n"
            f"Watching `{DASHBOARD_URL}` every {POLL_INTERVAL // 60} min.\n"
            f"Drawdown alert at {DRAWDOWN_ALERT_PCT}% | Stall alert after {NO_TRADES_ALERT_HOURS}h."
        )

        while True:
            try:
                snap = await self.fetch_snapshot()
                await self.check_and_alert(snap)

                if time.time() - self._last_daily_summary >= DAILY_INTERVAL:
                    await self.send_daily_summary(snap)
                    self._last_daily_summary = time.time()

            except Exception as e:
                print(f"Monitor loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    missing = [v for v in ("DASHBOARD_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(v)]
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}")
        raise SystemExit(1)
    asyncio.run(Monitor().run())
