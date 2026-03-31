"""
Kalshi exchange client.
Fetches markets and orderbooks from the Kalshi trading API.
Paper trading mode simulates order execution without touching real capital.
Gracefully degrades to empty data when no credentials are configured.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.utils.logger import logger
from src.utils.metrics import api_latency


def _rsa_sign(private_key_pem: str, timestamp_ms: int, method: str, path: str) -> str:
    """Sign a Kalshi API request using RSA-PSS SHA-256."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding
    message = f"{timestamp_ms}{method}{path}".encode()
    # Handle both raw PEM and escaped newlines from env vars
    pem = private_key_pem.replace("\\n", "\n").encode()
    private_key = serialization.load_pem_private_key(pem, password=None)
    signature = private_key.sign(
        message,
        crypto_padding.PSS(
            mgf=crypto_padding.MGF1(hashes.SHA256()),
            salt_length=crypto_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


# ------------------------------------------------------------------ #
#  Data types                                                          #
# ------------------------------------------------------------------ #

@dataclass
class KalshiMarket:
    ticker: str           # e.g. "KXBTCD-25DEC31-T100000"
    title: str            # human-readable question
    yes_bid: float        # 0-1 range
    yes_ask: float        # 0-1 range
    no_bid: float         # 0-1 range
    no_ask: float         # 0-1 range
    volume: float
    open_interest: float
    expiry_time: str      # ISO format
    category: str
    status: str


@dataclass
class KalshiOrderbook:
    ticker: str
    yes_bids: list[tuple[float, int]]   # (price 0-1, size)
    yes_asks: list[tuple[float, int]]   # (price 0-1, size)
    timestamp: float = field(default_factory=time.time)

    @property
    def best_yes_bid(self) -> float | None:
        """Highest resting bid price on the YES side."""
        return self.yes_bids[0][0] if self.yes_bids else None

    @property
    def best_yes_ask(self) -> float | None:
        """Lowest resting ask price on the YES side."""
        return self.yes_asks[0][0] if self.yes_asks else None

    @property
    def mid(self) -> float | None:
        """Mid-price between best bid and best ask."""
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return None


# ------------------------------------------------------------------ #
#  Client                                                              #
# ------------------------------------------------------------------ #

class KalshiClient:
    """
    Async Kalshi trading API client.

    Authentication priority:
      1. KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY env vars -> RSA-PSS signing (recommended).
      2. KALSHI_API_TOKEN env var  -> Bearer token used directly.
      3. KALSHI_EMAIL + KALSHI_PASSWORD env vars -> POST /login to obtain token.
      4. None present -> degraded mode (returns empty data, warns once).

    Use as an async context manager::

        async with KalshiClient(config, paper_trading=True) as client:
            markets = await client.get_markets_cached()
    """

    # Production vs demo — keys are environment-specific and not interchangeable.
    # Set KALSHI_DEMO=true if your API key was created on the demo environment.
    _PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
    _DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
    _CACHE_TTL = 30.0  # seconds

    def __init__(self, config, paper_trading: bool = True) -> None:
        self.config = config
        self.paper_trading = paper_trading

        _demo = os.getenv("KALSHI_DEMO", "").lower() in ("1", "true", "yes")
        self.API_BASE = self._DEMO_BASE if _demo else self._PROD_BASE

        self._key_id: str | None = os.getenv("KALSHI_API_KEY_ID")
        self._private_key: str | None = os.getenv("KALSHI_PRIVATE_KEY")
        self._token: str | None = os.getenv("KALSHI_API_TOKEN")
        self._email: str | None = os.getenv("KALSHI_EMAIL")
        self._password: str | None = os.getenv("KALSHI_PASSWORD")

        self._http: httpx.AsyncClient | None = None
        self._no_creds_warned: bool = False
        self._login_ts: float = 0.0          # when we last did email/password login
        _LOGIN_TTL = 3 * 3600                 # re-login every 3 h (tokens expire in ~6 h)
        self._LOGIN_TTL = _LOGIN_TTL

        # Market cache
        self._market_cache: list[KalshiMarket] = []
        self._cache_ts: float = 0.0
        # Last REST error (for dashboard / logs) — cleared on successful markets fetch
        self._last_error: str | None = None

        # Startup validation — log exactly what auth mode will be used
        self._log_auth_status()

    def _log_auth_status(self) -> None:
        """Log which auth method will be used, or exactly what's missing."""
        if self._key_id and self._private_key:
            # Validate the PEM key can actually be parsed
            try:
                from cryptography.hazmat.primitives import serialization
                pem = self._private_key.replace("\\n", "\n").encode()
                serialization.load_pem_private_key(pem, password=None)
                logger.info(f"KalshiClient: RSA auth ready (key_id={self._key_id[:8]}...)")
            except Exception as exc:
                logger.error(
                    f"KalshiClient: KALSHI_PRIVATE_KEY is set but failed to parse: {exc}. "
                    "Ensure the key is a valid RSA PEM and that newlines are stored as literal \\n "
                    "(two characters: backslash + n) in Railway env vars."
                )
        elif self._token:
            logger.info("KalshiClient: bearer token auth ready (KALSHI_API_TOKEN)")
        elif self._email and self._password:
            logger.info(f"KalshiClient: email/password auth ready ({self._email})")
        elif self._key_id and not self._private_key:
            logger.error(
                "KalshiClient: KALSHI_API_KEY_ID is set but KALSHI_PRIVATE_KEY is missing. "
                "Both env vars are required for RSA auth."
            )
        elif self._private_key and not self._key_id:
            logger.error(
                "KalshiClient: KALSHI_PRIVATE_KEY is set but KALSHI_API_KEY_ID is missing. "
                "Both env vars are required for RSA auth."
            )
        else:
            logger.warning(
                "KalshiClient: no credentials found. Set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY "
                "(RSA auth, recommended), KALSHI_API_TOKEN (bearer), or KALSHI_EMAIL + KALSHI_PASSWORD."
            )

    # ------------------------------------------------------------------ #
    #  Context manager                                                     #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "KalshiClient":
        await self._ensure_http()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _ensure_http(self) -> None:
        """Lazily create the HTTP client and authenticate. Safe to call multiple times.
        Re-logins via email/password when the session token is near expiry."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.API_BASE,
                timeout=httpx.Timeout(10.0),
                headers={"User-Agent": "polymarket-arb-bot/1.0"},
            )
        # Refresh email/password session token if it's stale (or was never acquired)
        if not self._key_id and self._email and self._password:
            if time.time() - self._login_ts > self._LOGIN_TTL:
                await self._login()

    # ------------------------------------------------------------------ #
    #  Auth helpers                                                        #
    # ------------------------------------------------------------------ #

    async def _login(self) -> None:
        """POST /login to exchange email+password for a session token."""
        try:
            resp = await self._http.post(
                "/login",
                json={"email": self._email, "password": self._password},
            )
            resp.raise_for_status()
            self._token = resp.json().get("token")
            if self._token:
                self._login_ts = time.time()
                logger.info("KalshiClient: authenticated via email/password login")
            else:
                logger.warning("KalshiClient: /login succeeded but no token in response")
        except Exception as exc:
            logger.warning(f"KalshiClient: login failed ({exc}); will operate in degraded mode")

    def _auth_headers(self, method: str = "GET", path: str = "/") -> dict[str, str]:
        # RSA key auth (recommended by Kalshi)
        if self._key_id and self._private_key:
            ts = int(time.time() * 1000)
            try:
                sig = _rsa_sign(self._private_key, ts, method.upper(), path)
                return {
                    "Kalshi-Access-Key": self._key_id,
                    "Kalshi-Access-Timestamp": str(ts),
                    "Kalshi-Access-Signature": sig,
                }
            except Exception as exc:
                logger.error(
                    f"KalshiClient: RSA signing failed ({exc}) — request will be sent without auth and will 401. "
                    "Check KALSHI_PRIVATE_KEY format."
                )
        # Bearer token fallback
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        if self._key_id or self._private_key:
            # Credentials are partially set but signing failed — log so it's visible
            logger.error(
                "KalshiClient: sending request with no auth headers — will get 401. "
                "Ensure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY are both set and valid."
            )
        return {}

    def _has_credentials(self) -> bool:
        """True if any auth method is configured (RSA, bearer token, or email/password)."""
        if self._key_id and self._private_key:
            return True
        if self._token:
            return True
        if self._email and self._password:
            return True
        return False

    def _warn_no_creds(self) -> None:
        if not self._no_creds_warned:
            logger.warning(
                "KalshiClient: no API credentials found — set one of: "
                "KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY (recommended), "
                "KALSHI_API_TOKEN, or KALSHI_EMAIL + KALSHI_PASSWORD. "
                "Returning empty data."
            )
            self._no_creds_warned = True

    # ------------------------------------------------------------------ #
    #  Market data                                                         #
    # ------------------------------------------------------------------ #

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    async def get_markets(
        self,
        limit: int = 100,
        status: str = "open",
    ) -> list[KalshiMarket]:
        """
        Fetch active markets from Kalshi.

        Returns an empty list (with a one-time warning) if credentials are
        absent, so the rest of the bot can continue without Kalshi data.
        """
        if not self._has_credentials():
            self._warn_no_creds()
            self._last_error = "no_credentials"
            return []

        await self._ensure_http()
        start = time.perf_counter()
        try:
            resp = await self._http.get(
                "/markets",
                params={"limit": limit, "status": status},
                headers=self._auth_headers("GET", "/trade-api/v2/markets"),
            )
            resp.raise_for_status()
            self._last_error = None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401 and self._email and self._password:
                # Token expired — force re-login and retry once
                logger.warning("KalshiClient: 401 on markets, re-logging in...")
                self._login_ts = 0.0
                await self._ensure_http()
                try:
                    resp = await self._http.get(
                        "/markets",
                        params={"limit": limit, "status": status},
                        headers=self._auth_headers("GET", "/trade-api/v2/markets"),
                    )
                    resp.raise_for_status()
                    self._last_error = None
                except Exception as retry_exc:
                    self._last_error = f"markets_retry: {retry_exc!s}"
                    logger.warning("KalshiClient: re-login retry also failed; returning []")
                    return []
            elif exc.response.status_code in (401, 403):
                self._last_error = f"markets_http_{exc.response.status_code}: {exc!s}"
                _body = exc.response.text[:300]
                logger.error(
                    f"KalshiClient: {exc.response.status_code} fetching markets. "
                    f"Kalshi response: {_body}. "
                    "Common causes: (1) demo key used against production — set KALSHI_DEMO=true if your key is from demo-api.kalshi.co; "
                    "(2) KALSHI_PRIVATE_KEY newlines broken in env vars; "
                    "(3) wrong KALSHI_API_KEY_ID."
                )
                return []
            else:
                self._last_error = f"markets_http_{exc.response.status_code}: {exc!s}"
                raise
        finally:
            elapsed = time.perf_counter() - start
            api_latency.labels(endpoint="kalshi_get_markets").observe(elapsed)

        markets: list[KalshiMarket] = []
        for raw in resp.json().get("markets", []):
            try:
                markets.append(_parse_market(raw))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                logger.debug(f"KalshiClient: skipping malformed market entry: {exc}")

        logger.info(f"KalshiClient: fetched {len(markets)} markets")
        return markets

    async def get_markets_cached(self) -> list[KalshiMarket]:
        """Return markets from cache, refreshing if the cache is stale (30 s TTL)."""
        now = time.time()
        if now - self._cache_ts > self._CACHE_TTL or not self._market_cache:
            self._market_cache = await self.get_markets(limit=500)
            self._cache_ts = now
            logger.debug(
                f"KalshiClient: refreshed market cache ({len(self._market_cache)} markets)"
            )
        return list(self._market_cache)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=0.5, max=3))
    async def get_orderbook(self, ticker: str) -> KalshiOrderbook | None:
        """
        Fetch the YES/NO orderbook for *ticker*.

        Returns None if credentials are absent or the request fails gracefully.
        Kalshi prices arrive in cents (0-99); they are divided by 100 before
        being stored so all downstream code works in the 0-1 range.
        """
        if not self._has_credentials():
            self._warn_no_creds()
            return None

        await self._ensure_http()
        start = time.perf_counter()
        try:
            resp = await self._http.get(
                f"/markets/{ticker}/orderbook",
                headers=self._auth_headers("GET", f"/trade-api/v2/markets/{ticker}/orderbook"),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.debug(f"KalshiClient: orderbook not found for ticker={ticker}")
                return None
            if exc.response.status_code in (401, 403):
                logger.warning(f"KalshiClient: auth error fetching orderbook ({exc})")
                return None
            raise
        finally:
            elapsed = time.perf_counter() - start
            api_latency.labels(endpoint="kalshi_get_orderbook").observe(elapsed)

        data = resp.json().get("orderbook", resp.json())
        return _parse_orderbook(ticker, data)


# ------------------------------------------------------------------ #
#  Parsing helpers                                                     #
# ------------------------------------------------------------------ #

def _parse_market(raw: dict) -> KalshiMarket:
    """
    Convert a raw Kalshi market dict into a KalshiMarket dataclass.

    The new api.elections.kalshi.com API returns prices as dollar strings
    in the 0-1 range (e.g. "0.5500") via the *_dollars fields.
    The old trading-api.kalshi.com API returned prices in cents (integers).
    We support both: prefer *_dollars fields, fall back to cent fields.
    """
    def _price(dollars_val, cents_val=None) -> float:
        # New API: *_dollars fields, already in 0-1 range as strings/floats
        if dollars_val is not None:
            return float(dollars_val or 0)
        # Old API: integer cents
        if cents_val is not None:
            return float(cents_val or 0) / 100.0
        return 0.0

    return KalshiMarket(
        ticker=str(raw.get("ticker", "")),
        title=str(raw.get("title", "")),
        yes_bid=_price(raw.get("yes_bid_dollars"), raw.get("yes_bid")),
        yes_ask=_price(raw.get("yes_ask_dollars"), raw.get("yes_ask")),
        no_bid=_price(raw.get("no_bid_dollars"), raw.get("no_bid")),
        no_ask=_price(raw.get("no_ask_dollars"), raw.get("no_ask")),
        volume=float(raw.get("volume_fp") or raw.get("volume") or 0),
        open_interest=float(raw.get("open_interest_fp") or raw.get("open_interest") or 0),
        expiry_time=str(raw.get("expiration_time") or raw.get("close_time", "")),
        category=str(raw.get("category", "")),
        status=str(raw.get("status", "")),
    )


def _parse_orderbook(ticker: str, data: dict) -> KalshiOrderbook:
    """
    Build a KalshiOrderbook from raw API data.

    The Kalshi orderbook payload looks like::

        {
          "yes": [[price_cents, size], ...],   # sorted descending
          "no":  [[price_cents, size], ...]    # sorted descending
        }

    We expose only the YES side (bids and asks) in the 0-1 range.
    The YES ask prices are derived from the NO bids: yes_ask = 1 - no_bid.
    """
    def _to_01(price_cents: int | float) -> float:
        return float(price_cents) / 100.0

    raw_yes: list[list] = data.get("yes", [])
    raw_no: list[list] = data.get("no", [])

    # YES bids: resting buy orders on YES, sorted best (highest) first.
    yes_bids: list[tuple[float, int]] = sorted(
        [(_to_01(row[0]), int(row[1])) for row in raw_yes if len(row) >= 2],
        key=lambda x: x[0],
        reverse=True,
    )

    # YES asks are implicitly the complement of NO bids:
    # if someone bids 55 cents on NO, the implied YES ask is 45 cents.
    yes_asks: list[tuple[float, int]] = sorted(
        [(round(1.0 - _to_01(row[0]), 6), int(row[1])) for row in raw_no if len(row) >= 2],
        key=lambda x: x[0],
    )

    return KalshiOrderbook(
        ticker=ticker,
        yes_bids=yes_bids,
        yes_asks=yes_asks,
        timestamp=time.time(),
    )
