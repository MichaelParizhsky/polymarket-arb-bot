"""
WebSocket-based live orderbook feed for Polymarket.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market and maintains
an in-memory cache of orderbooks, updated in real-time via the Polymarket
CLOB WebSocket subscription protocol.

Protocol summary:
  - Connect, then send subscription message once
  - Receive "book" (full snapshot) and "price_change" (delta) messages
  - "book": full orderbook replacement for a token
  - "price_change": incremental changes; size "0" removes a price level
"""
from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from typing import Any

import websockets
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.exchange.polymarket import Orderbook, OrderbookLevel
from src.utils.logger import logger

# ------------------------------------------------------------------ #
#  Constants                                                           #
# ------------------------------------------------------------------ #

WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 20          # seconds between WS pings
PING_TIMEOUT = 10           # seconds to wait for pong
RECONNECT_DELAY_MIN = 1     # minimum reconnect backoff (seconds)
RECONNECT_DELAY_MAX = 30    # maximum reconnect backoff (seconds)
MAX_RECONNECT_ATTEMPTS = 10  # attempts per connect cycle before backing off longer


# ------------------------------------------------------------------ #
#  Internal mutable orderbook representation                           #
# ------------------------------------------------------------------ #

class _MutableOrderbook:
    """
    In-memory orderbook for a single token, supports snapshot replacement
    and incremental price-level updates.
    """

    __slots__ = ("token_id", "bids", "asks", "last_update")

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        # price -> size (both stored as float)
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update: float = 0.0

    # --- snapshot ---

    def apply_snapshot(self, raw_bids: list[dict], raw_asks: list[dict]) -> None:
        self.bids = {}
        self.asks = {}
        for b in raw_bids:
            try:
                price = float(b["price"])
                size = float(b["size"])
                if size > 0:
                    self.bids[price] = size
            except (KeyError, ValueError):
                pass
        for a in raw_asks:
            try:
                price = float(a["price"])
                size = float(a["size"])
                if size > 0:
                    self.asks[price] = size
            except (KeyError, ValueError):
                pass
        self.last_update = time.time()

    # --- delta ---

    def apply_price_change(self, changes: list[list[str]]) -> None:
        """
        Each change is [side, price, size].
        Side is "BUY" (bid) or "SELL" (ask).
        Size "0" means remove the level.
        """
        for change in changes:
            try:
                side, price_str, size_str = change[0], change[1], change[2]
                price = float(price_str)
                size = float(size_str)
                book = self.bids if side == "BUY" else self.asks
                if size == 0.0:
                    book.pop(price, None)
                else:
                    book[price] = size
            except (IndexError, ValueError):
                pass
        self.last_update = time.time()

    # --- snapshot export ---

    def to_orderbook(self) -> Orderbook:
        bids = sorted(
            [OrderbookLevel(price=p, size=s) for p, s in self.bids.items()],
            key=lambda x: x.price,
            reverse=True,
        )
        asks = sorted(
            [OrderbookLevel(price=p, size=s) for p, s in self.asks.items()],
            key=lambda x: x.price,
        )
        return Orderbook(
            token_id=self.token_id,
            bids=bids,
            asks=asks,
            timestamp=self.last_update,
        )


# ------------------------------------------------------------------ #
#  Main feed class                                                      #
# ------------------------------------------------------------------ #

class PolymarketWSFeed:
    """
    Maintains a live in-memory orderbook cache for a set of Polymarket tokens
    via the CLOB WebSocket feed.

    Usage:
        feed = PolymarketWSFeed()
        feed.subscribe(["token_id_1", "token_id_2"])
        await feed.start()
        ...
        book = feed.get_orderbook("token_id_1")
        if book and not feed.is_stale("token_id_1"):
            ...
        await feed.stop()
    """

    def __init__(self) -> None:
        # live cache: token_id -> _MutableOrderbook
        self._books: dict[str, _MutableOrderbook] = {}
        # token_ids we want to subscribe to (set on subscribe())
        self._token_ids: list[str] = []
        self._running = False
        self._ws_task: asyncio.Task | None = None
        # Lock protects _books during concurrent read/write
        self._lock = asyncio.Lock()
        # Live reference to active WS connection for dynamic re-subscription
        self._active_ws: Any | None = None
        # Queue of token_id batches to subscribe to on the active connection
        self._pending_sub: asyncio.Queue = asyncio.Queue()

    # ---------------------------------------------------------------- #
    #  Public API                                                        #
    # ---------------------------------------------------------------- #

    def subscribe(self, token_ids: list[str]) -> None:
        """
        Register token IDs to subscribe to on (re-)connect.
        Can be called before or after start(); if a connection is active,
        the new tokens are also sent immediately on the live socket.
        """
        new = [tid for tid in token_ids if tid not in self._token_ids]
        self._token_ids.extend(new)
        # Do NOT pre-create empty books — empty books are indistinguishable
        # from real data and will cause REST fallback to be skipped.
        if new:
            # Push to live connection if active; consumed by _drain_pending_subs
            self._pending_sub.put_nowait(new)
            logger.debug(
                f"[PolymarketWS] Queued {len(new)} new token subscriptions "
                f"(total={len(self._token_ids)})"
            )

    def get_orderbook(self, token_id: str) -> Orderbook | None:
        """
        Return a snapshot of the cached orderbook for the given token.
        Returns None if we have never received any data for this token.
        """
        mbook = self._books.get(token_id)
        if mbook is None:
            return None
        # Return a snapshot even if empty — callers can check is_stale()
        return mbook.to_orderbook()

    def is_stale(self, token_id: str, max_age: float = 3.0) -> bool:
        """
        Return True if the orderbook for token_id has not been updated
        within max_age seconds, or if we have never received data for it.
        """
        mbook = self._books.get(token_id)
        if mbook is None or mbook.last_update == 0.0:
            return True
        return (time.time() - mbook.last_update) > max_age

    async def start(self) -> None:
        """Start the background WebSocket task."""
        if self._running:
            logger.warning("[PolymarketWS] Already running — ignoring start()")
            return
        self._running = True
        self._ws_task = asyncio.create_task(self._run_forever())
        logger.info(
            f"[PolymarketWS] Feed started "
            f"(endpoint={WS_ENDPOINT}, tokens={len(self._token_ids)})"
        )

    async def stop(self) -> None:
        """Gracefully stop the feed."""
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        logger.info("[PolymarketWS] Feed stopped")

    # ---------------------------------------------------------------- #
    #  Internal connection loop                                          #
    # ---------------------------------------------------------------- #

    async def _run_forever(self) -> None:
        """
        Reconnect loop with exponential backoff via tenacity.
        Falls back gracefully after MAX_RECONNECT_ATTEMPTS consecutive
        failures (logs a warning and stops trying so the bot can keep
        running using REST-based orderbooks).
        """
        fail_count = 0
        while self._running:
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception_type(Exception),
                    stop=stop_after_attempt(MAX_RECONNECT_ATTEMPTS),
                    wait=wait_exponential(
                        min=RECONNECT_DELAY_MIN, max=RECONNECT_DELAY_MAX
                    ),
                    reraise=True,
                ):
                    with attempt:
                        if not self._running:
                            return
                        await self._connect()
                fail_count = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                fail_count += 1
                logger.warning(
                    f"[PolymarketWS] WS unavailable after {MAX_RECONNECT_ATTEMPTS} "
                    f"attempts ({exc}). "
                    "Bot will continue using REST-based orderbooks. "
                    f"Retrying in {RECONNECT_DELAY_MAX}s..."
                )
                # Long sleep before restarting the whole retry cycle
                try:
                    await asyncio.sleep(RECONNECT_DELAY_MAX)
                except asyncio.CancelledError:
                    return

    async def _connect(self) -> None:
        """
        Open a single WebSocket connection, send the subscription message,
        and process messages until disconnected.
        """
        logger.debug(f"[PolymarketWS] Connecting to {WS_ENDPOINT}")
        async with websockets.connect(
            WS_ENDPOINT,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            open_timeout=15,
        ) as ws:
            self._active_ws = ws
            logger.info(
                f"[PolymarketWS] Connected. Subscribing to "
                f"{len(self._token_ids)} token(s)."
            )
            await self._send_subscription(ws)
            # Concurrently: drain pending subscriptions + receive messages
            await asyncio.gather(
                self._drain_pending_subs(ws),
                self._receive_loop(ws),
                return_exceptions=True,
            )
            self._active_ws = None

    async def _receive_loop(self, ws: Any) -> None:
        """Read and dispatch messages from the active connection."""
        async for raw in ws:
            if not self._running:
                break
            try:
                await self._handle_raw(raw)
            except Exception as exc:
                logger.debug(f"[PolymarketWS] Message handling error: {exc}")

    async def _drain_pending_subs(self, ws: Any) -> None:
        """
        Send subscription messages for any token_ids queued after connect.
        Runs concurrently with _receive_loop; exits when WS closes.
        """
        while self._running:
            try:
                new_tokens = await asyncio.wait_for(
                    self._pending_sub.get(), timeout=1.0
                )
                msg = json.dumps({
                    "auth": {},
                    "type": "market",
                    "assets_ids": new_tokens,
                })
                await ws.send(msg)
                logger.debug(
                    f"[PolymarketWS] Live-subscribed {len(new_tokens)} new tokens"
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    async def _send_subscription(self, ws: Any) -> None:
        """Send the Polymarket market subscription message."""
        if not self._token_ids:
            logger.debug("[PolymarketWS] No token IDs to subscribe to yet.")
            return
        msg = json.dumps(
            {
                "auth": {},
                "type": "market",
                "assets_ids": self._token_ids,
            }
        )
        await ws.send(msg)
        logger.debug(
            f"[PolymarketWS] Subscription sent for {len(self._token_ids)} tokens"
        )

    # ---------------------------------------------------------------- #
    #  Message dispatch                                                  #
    # ---------------------------------------------------------------- #

    async def _handle_raw(self, raw: str | bytes) -> None:
        """Parse a raw WebSocket message and dispatch to the appropriate handler."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        # Polymarket can send a JSON array of events or a single event object
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.debug(f"[PolymarketWS] JSON decode error: {exc}")
            return

        if isinstance(data, list):
            for event in data:
                await self._dispatch(event)
        elif isinstance(data, dict):
            await self._dispatch(data)

    async def _dispatch(self, event: dict) -> None:
        """Route a single event dict to the correct handler."""
        msg_type = event.get("event_type") or event.get("type", "")
        if msg_type == "book":
            await self._handle_book(event)
        elif msg_type == "price_change":
            await self._handle_price_change(event)
        elif msg_type in ("last_trade_price", "tick_size_change"):
            # Informational — no orderbook impact
            pass
        else:
            logger.debug(f"[PolymarketWS] Unknown message type: {msg_type!r}")

    # ---------------------------------------------------------------- #
    #  Book snapshot handler                                             #
    # ---------------------------------------------------------------- #

    async def _handle_book(self, event: dict) -> None:
        """
        Full orderbook snapshot.
        Expected shape:
          {
            "type": "book",
            "asset_id": "<token_id>",
            "market": "<condition_id>",
            "bids": [{"price": "0.45", "size": "100"}, ...],
            "asks": [{"price": "0.55", "size": "80"}, ...]
          }
        """
        token_id = (
            event.get("asset_id")
            or event.get("token_id")
            or event.get("id")
            or ""
        )
        if not token_id:
            logger.debug(f"[PolymarketWS] book missing asset_id — keys: {list(event.keys())}")
            return

        raw_bids = event.get("bids", [])
        raw_asks = event.get("asks", [])

        async with self._lock:
            mbook = self._books.get(token_id)
            if mbook is None:
                mbook = _MutableOrderbook(token_id)
                self._books[token_id] = mbook
            mbook.apply_snapshot(raw_bids, raw_asks)

        logger.debug(
            f"[PolymarketWS] BOOK snapshot | token={token_id[:16]}... | "
            f"bids={len(raw_bids)} asks={len(raw_asks)}"
        )

    # ---------------------------------------------------------------- #
    #  Price-change delta handler                                        #
    # ---------------------------------------------------------------- #

    async def _handle_price_change(self, event: dict) -> None:
        """
        Incremental orderbook update.

        Two known sub-formats:

        1. `changes` array (preferred):
           {
             "type": "price_change",
             "asset_id": "<token_id>",
             "changes": [["BUY", "0.45", "100"], ["SELL", "0.55", "0"], ...]
           }

        2. Flat fields (legacy):
           {
             "type": "price_change",
             "asset_id": "<token_id>",
             "side": "BUY" | "SELL",
             "price": "0.45",
             "size": "100",
             "market": "<condition_id>"
           }
        """
        token_id = (
            event.get("asset_id")
            or event.get("token_id")
            or event.get("id")
            or ""
        )

        # New format: top-level market + price_changes list per asset
        if not token_id and "price_changes" in event:
            await self._handle_price_changes_batch(event)
            return

        if not token_id:
            logger.debug(f"[PolymarketWS] price_change missing asset_id — keys: {list(event.keys())}")
            return

        changes: list[list[str]] = []

        if "changes" in event:
            changes = event["changes"]
        elif "side" in event and "price" in event and "size" in event:
            changes = [[event["side"], event["price"], event["size"]]]
        else:
            logger.debug(
                f"[PolymarketWS] price_change for {token_id[:16]} has no "
                "recognised change payload"
            )
            return

        async with self._lock:
            mbook = self._books.get(token_id)
            if mbook is None:
                # We received a delta before a snapshot — create a fresh book;
                # the next snapshot will reconcile it.
                mbook = _MutableOrderbook(token_id)
                self._books[token_id] = mbook
            mbook.apply_price_change(changes)

        logger.debug(
            f"[PolymarketWS] DELTA | token={token_id[:16]}... | "
            f"{len(changes)} change(s)"
        )

    async def _handle_price_changes_batch(self, event: dict) -> None:
        """
        Handle the newer Polymarket WS format:
        {
          "event_type": "price_change",
          "market": "<condition_id>",
          "timestamp": "...",
          "price_changes": [
            {"asset_id": "<token_id>", "side": "BUY", "price": "0.45", "size": "100"},
            ...
          ]
        }
        """
        price_changes = event.get("price_changes", [])
        async with self._lock:
            for change in price_changes:
                token_id = change.get("asset_id") or change.get("token_id", "")
                if not token_id:
                    continue
                side = change.get("side", "")
                price = change.get("price", "")
                size = change.get("size", "0")
                if side and price:
                    mbook = self._books.get(token_id)
                    if mbook is None:
                        mbook = _MutableOrderbook(token_id)
                        self._books[token_id] = mbook
                    mbook.apply_price_change([[side, price, size]])
        logger.debug(f"[PolymarketWS] BATCH DELTA | {len(price_changes)} change(s)")


# ------------------------------------------------------------------ #
#  User channel — authenticated real-time fill feed                   #
# ------------------------------------------------------------------ #

WS_USER_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class PolymarketUserWSFeed:
    """
    Authenticated WebSocket feed for user-level events.

    Connects to the Polymarket user channel and fires callbacks when:
      - An order is filled ("trade" event)
      - An order status changes ("order" event)

    Unlike the market feed (orderbooks), this channel requires API credentials
    and gives real-time fill data so the bot doesn't have to infer fills from
    position state changes.

    Usage::
        feed = PolymarketUserWSFeed(api_key, api_secret, api_passphrase)
        feed.on_fill(my_fill_handler)        # async or sync callback
        feed.subscribe_market(condition_id)  # add markets to monitor
        await feed.start()
        ...
        await feed.stop()
    """

    def __init__(self, api_key: str, api_secret: str, api_passphrase: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._running = False
        self._ws_task: asyncio.Task | None = None
        self._active_ws: Any = None
        self._markets: set[str] = set()
        self._fill_callbacks: list = []
        self._order_callbacks: list = []

    # ── Public API ──────────────────────────────────────────────────────

    def on_fill(self, callback) -> None:
        """Register a callback fired on trade fill events.
        Callback receives the raw event dict.  Can be async or sync."""
        self._fill_callbacks.append(callback)

    def on_order_update(self, callback) -> None:
        """Register a callback fired on order status-change events."""
        self._order_callbacks.append(callback)

    def subscribe_market(self, condition_id: str) -> None:
        """Add a market (by condition_id) to the subscription set."""
        self._markets.add(condition_id)

    async def start(self) -> None:
        """Start the background WebSocket task."""
        if self._running:
            return
        if not self._api_key:
            logger.warning("[PolymarketUserWS] No API key — user feed disabled")
            return
        self._running = True
        self._ws_task = asyncio.create_task(self._run_forever())
        logger.info("[PolymarketUserWS] Feed started")

    async def stop(self) -> None:
        """Gracefully stop the feed."""
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        logger.info("[PolymarketUserWS] Feed stopped")

    # ── Internal ────────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        while self._running:
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception_type(Exception),
                    stop=stop_after_attempt(MAX_RECONNECT_ATTEMPTS),
                    wait=wait_exponential(
                        min=RECONNECT_DELAY_MIN, max=RECONNECT_DELAY_MAX
                    ),
                    reraise=True,
                ):
                    with attempt:
                        if not self._running:
                            return
                        await self._connect()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    f"[PolymarketUserWS] Unavailable after {MAX_RECONNECT_ATTEMPTS} "
                    f"attempts ({exc}). Retrying in {RECONNECT_DELAY_MAX}s..."
                )
                try:
                    await asyncio.sleep(RECONNECT_DELAY_MAX)
                except asyncio.CancelledError:
                    return

    async def _connect(self) -> None:
        logger.debug(f"[PolymarketUserWS] Connecting to {WS_USER_ENDPOINT}")
        async with websockets.connect(
            WS_USER_ENDPOINT,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            open_timeout=15,
        ) as ws:
            self._active_ws = ws
            logger.info("[PolymarketUserWS] Connected — sending auth + subscription")
            await ws.send(json.dumps({
                "auth": {
                    "apiKey": self._api_key,
                    "secret": self._api_secret,
                    "passphrase": self._api_passphrase,
                },
                "type": "user",
                "markets": list(self._markets),
            }))
            async for raw in ws:
                if not self._running:
                    break
                try:
                    await self._handle_message(raw)
                except Exception as exc:
                    logger.debug(f"[PolymarketUserWS] Message error: {exc}")
            self._active_ws = None

    async def _handle_message(self, raw: str) -> None:
        """Parse and dispatch a user channel message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Messages can arrive as a single object or a list
        msgs: list[dict] = data if isinstance(data, list) else [data]

        for msg in msgs:
            event_type = msg.get("event_type") or msg.get("type")

            if event_type == "trade":
                logger.info(
                    f"[PolymarketUserWS] Fill: asset={msg.get('asset_id', '')[:16]} "
                    f"side={msg.get('side')} size={msg.get('size')} price={msg.get('price')}"
                )
                for cb in self._fill_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            await cb(msg)
                        else:
                            cb(msg)
                    except Exception as exc:
                        logger.debug(f"[PolymarketUserWS] fill callback error: {exc}")

            elif event_type == "order":
                logger.debug(
                    f"[PolymarketUserWS] Order update: id={msg.get('order_id', '')[:16]} "
                    f"status={msg.get('status')}"
                )
                for cb in self._order_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            await cb(msg)
                        else:
                            cb(msg)
                    except Exception as exc:
                        logger.debug(f"[PolymarketUserWS] order callback error: {exc}")
