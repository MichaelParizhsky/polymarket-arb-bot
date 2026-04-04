"""
Enhanced FastAPI dashboard with tabs for bot + meta-agent.
Visit http://localhost:5000
"""
from __future__ import annotations

import asyncio
import collections
import glob
import json
import os
import re
import time
import threading as _threading

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from src.utils.logger import logger

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

# Optional API key for destructive endpoints (set DASHBOARD_API_KEY env var to enable)
_DASHBOARD_API_KEY: str = os.getenv("DASHBOARD_API_KEY", "")


def _check_api_key(x_api_key: str = Header(default="")) -> bool:
    """Return True if request is authorized.
    If DASHBOARD_API_KEY is not set, all requests are allowed (open access).
    If set, the header must match."""
    if not _DASHBOARD_API_KEY:
        return True
    return x_api_key == _DASHBOARD_API_KEY

import os as _os
_peer_bot_url: str = _os.getenv("PEER_BOT_URL", "").rstrip("/")
_bot_name: str = _os.getenv("BOT_NAME", "Bot A")

_portfolio = None
_bot_start_time = time.time()
_cycle_count = 0
_config = None
_risk = None
_binance_ref = None
_kalshi_ref = None
_news_monitor_ref = None
_hedge_manager_ref = None
_state_loaded_from_file: bool = False

# Lock protecting reads of the portfolio object from the dashboard thread
_portfolio_lock = _threading.RLock()

# Live market status cache: token_id -> {active, closed, end_date_iso, category}
# Updated by main.py after each market scan; used to enrich /api/positions.
_market_status: dict[str, dict] = {}
_market_status_lock = _threading.Lock()


def update_market_status(token_id: str, active: bool, closed: bool, end_date_iso: str, category: str = "") -> None:
    """Called by main.py to keep live market status for open positions."""
    with _market_status_lock:
        _market_status[token_id] = {
            "active": active,
            "closed": closed,
            "end_date_iso": end_date_iso,
            "category": category,
        }


# ── Sports live scores ──────────────────────────────────────────────────────
# ESPN's unofficial scoreboard API — free, no auth, ~30s latency during games.
# Endpoint pattern: https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
import re as _re

# Master sports keyword regex — shared by sports_stats and sports_live endpoints.
# Keyword approach: any market whose question matches is treated as a sports bet.
# Covers: major US leagues, European soccer leagues, team names, and bet-type terms.
_SPORTS_KW_MASTER = _re.compile(
    r"\b("
    # ── US leagues ───────────────────────────────────────────────────────────
    r"nba|nfl|nhl|mlb|mls|ncaa|cbb|ncaab|ncaaf|cfb|ufc|boxing|pga|nascar|wnba|"
    # ── European soccer competitions ─────────────────────────────────────────
    r"premier league|champions league|europa league|conference league|"
    r"la liga|bundesliga|serie a|ligue 1|eredivisie|primeira liga|"
    r"fa cup|copa del rey|dfb.?pokal|coppa italia|coupe de france|"
    r"epl|ucl|uel|uecl|"
    # ── Soccer generic ────────────────────────────────────────────────────────
    r"soccer|football club|futbol|"
    # ── Premier League clubs ──────────────────────────────────────────────────
    r"arsenal|chelsea|liverpool|tottenham|spurs|everton|leicester|"
    r"west ham|newcastle|wolves|wolverhampton|crystal palace|brentford|"
    r"aston villa|brighton|southampton|nottingham|fulham|burnley|"
    r"manchester city|manchester united|man city|man united|man utd|"
    # ── La Liga ───────────────────────────────────────────────────────────────
    r"real madrid|barcelona|atletico|atletico madrid|sevilla|valencia|villarreal|"
    r"real sociedad|athletic bilbao|betis|mallorca|"
    # ── Bundesliga ────────────────────────────────────────────────────────────
    r"bayern munich|borussia dortmund|bvb|rb leipzig|bayer leverkusen|"
    r"eintracht frankfurt|wolfsburg|freiburg|union berlin|"
    # ── Serie A ───────────────────────────────────────────────────────────────
    r"juventus|ac milan|inter milan|napoli|roma|lazio|atalanta|fiorentina|"
    # ── Ligue 1 ───────────────────────────────────────────────────────────────
    r"psg|paris saint.germain|olympique lyonnais|marseille|monaco|"
    # ── Other European ────────────────────────────────────────────────────────
    r"ajax|porto|benfica|sporting cp|celtic|rangers fc|psv|"
    # ── MLS clubs ─────────────────────────────────────────────────────────────
    r"inter miami|lafc|la galaxy|seattle sounders|portland timbers|"
    r"atlanta united|nycfc|new york red bulls|new england revolution|"
    r"toronto fc|vancouver whitecaps|"
    # ── NBA team nicknames ────────────────────────────────────────────────────
    r"celtics|lakers|warriors|bulls|heat|nets|knicks|sixers|bucks|raptors|"
    r"nuggets|thunder|clippers|mavericks|mavs|rockets|grizzlies|pelicans|"
    r"hawks|hornets|pistons|pacers|cavaliers|cavs|wizards|blazers|jazz|kings|"
    r"timberwolves|wolves|magic|suns|"
    # ── NFL team nicknames ────────────────────────────────────────────────────
    r"chiefs|eagles|patriots|cowboys|ravens|49ers|niners|seahawks|bills|"
    r"bengals|steelers|broncos|packers|bears|lions|vikings|buccaneers|bucs|"
    r"saints|falcons|texans|colts|jaguars|titans|commanders|dolphins|raiders|"
    r"chargers|giants|rams|cardinals|panthers|"
    # ── NHL team nicknames ────────────────────────────────────────────────────
    r"bruins|canadiens|maple leafs|penguins|flyers|capitals|hurricanes|"
    r"lightning|avalanche|oilers|flames|canucks|predators|blackhawks|"
    r"red wings|golden knights|kraken|"
    # ── MLB team nicknames ────────────────────────────────────────────────────
    r"yankees|red sox|dodgers|astros|braves|phillies|mariners|padres|"
    r"cubs|mets|orioles|rays|blue jays|rockies|"
    # ── Generic sports terms ──────────────────────────────────────────────────
    r"basketball|baseball|hockey|tennis|golf|rugby|cricket|"
    r"super bowl|stanley cup|world series|march madness|playoffs|"
    r"touchdown|strikeout|slam dunk|hat trick|clean sheet|penalty kick|"
    r"spread|over.under|moneyline|point spread"
    r")\b",
    _re.IGNORECASE,
)

# Strategies that can produce sports trades
_SPORTS_STRATEGIES = frozenset({
    "quick_resolution", "event_driven", "live_game", "resolution", "swarm_prediction",
})

_ESPN_URLS: dict[str, str] = {
    "nba":        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "nfl":        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "nhl":        "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "mlb":        "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "ncaaf":      "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "cfb":        "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "ncaab":      "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
    "cbb":        "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
    "mls":        "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard",
    "epl":        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
    "bundesliga": "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard",
    "laliga":     "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
    "seriea":     "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard",
    "ligue1":     "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/scoreboard",
    "ucl":        "https://site.api.espn.com/apis/site/v2/sports/soccer/UEFA.CHAMPIONS/scoreboard",
}

# Category strings from Polymarket → ESPN sport key
_SPORT_ALIASES: dict[str, str] = {
    "nba": "nba", "nfl": "nfl", "nhl": "nhl", "mlb": "mlb",
    "ncaaf": "ncaaf", "cfb": "ncaaf",
    "ncaab": "ncaab", "cbb": "ncaab",
    "mls": "mls",
    "epl": "epl", "premier league": "epl",
    "bundesliga": "bundesliga",
    "la liga": "laliga", "laliga": "laliga",
    "serie a": "seriea", "seriea": "seriea",
    "ligue 1": "ligue1", "ligue1": "ligue1",
    "champions league": "ucl", "ucl": "ucl",
    "soccer": "epl",         # default soccer → check EPL first
    "football": "nfl",       # American football default
    "basketball": "nba",
    "hockey": "nhl",
    "baseball": "mlb",
}

_SCORE_CACHE: dict[str, dict] = {}   # sport_key -> {ts: float, events: list}
_SCORE_CACHE_TTL = 30.0              # seconds

_TEAM_PAT = [
    _re.compile(r"Will (?:the )?(.+?) (?:beat|defeat|win(?:\s+(?:against|vs\.?))?|cover|top|upset) (?:the )?(.+?)[\?\.,$]", _re.I),
    _re.compile(r"^(.+?) (?:vs\.?|versus|@|at) (.+?)[\?\.,$]", _re.I),
    _re.compile(r"^(.+?) (?:vs\.?|versus|@|at) (.+?)$", _re.I),
]


def _extract_teams(question: str) -> tuple[str, str] | None:
    """Parse two team names out of a Polymarket question string."""
    q = question.strip()
    for pat in _TEAM_PAT:
        m = pat.search(q)
        if m:
            t1 = m.group(1).strip().strip("?.,")
            t2 = m.group(2).strip().strip("?.,")
            # Drop trailing noise like "Game 1", "- winner", etc.
            for noise in [" Game 1", " Game 2", " Game 3", " - winner", " winner", " tonight"]:
                t1 = t1.replace(noise, "").strip()
                t2 = t2.replace(noise, "").strip()
            if t1 and t2:
                return t1, t2
    return None


# Team nickname/city → sport lookup.
# Used when Polymarket category is generic ("Sports", "") and question has no league keyword.
_TEAM_SPORT: dict[str, str] = {
    # NBA
    "magic": "nba", "suns": "nba", "lakers": "nba", "celtics": "nba",
    "warriors": "nba", "bulls": "nba", "heat": "nba", "nets": "nba",
    "knicks": "nba", "sixers": "nba", "76ers": "nba", "bucks": "nba",
    "raptors": "nba", "nuggets": "nba", "thunder": "nba", "clippers": "nba",
    "mavericks": "nba", "mavs": "nba", "rockets": "nba", "spurs": "nba",
    "grizzlies": "nba", "pelicans": "nba", "hawks": "nba", "hornets": "nba",
    "pistons": "nba", "pacers": "nba", "cavaliers": "nba", "cavs": "nba",
    "wizards": "nba", "blazers": "nba", "timberwolves": "nba", "wolves": "nba",
    "jazz": "nba", "kings": "nba",
    # NFL
    "chiefs": "nfl", "eagles": "nfl", "patriots": "nfl", "cowboys": "nfl",
    "ravens": "nfl", "49ers": "nfl", "niners": "nfl", "rams": "nfl",
    "seahawks": "nfl", "bills": "nfl", "bengals": "nfl", "browns": "nfl",
    "steelers": "nfl", "broncos": "nfl", "raiders": "nfl", "chargers": "nfl",
    "packers": "nfl", "bears": "nfl", "lions": "nfl", "vikings": "nfl",
    "buccaneers": "nfl", "bucs": "nfl", "saints": "nfl", "falcons": "nfl",
    "panthers": "nfl", "texans": "nfl", "colts": "nfl", "jaguars": "nfl",
    "titans": "nfl", "cardinals": "nfl", "giants": "nfl", "commanders": "nfl",
    "dolphins": "nfl",
    # NHL
    "bruins": "nhl", "canadiens": "nhl", "habs": "nhl", "maple leafs": "nhl",
    "leafs": "nhl", "rangers": "nhl", "penguins": "nhl", "pens": "nhl",
    "flyers": "nhl", "capitals": "nhl", "caps": "nhl", "hurricanes": "nhl",
    "canes": "nhl", "lightning": "nhl", "bolts": "nhl",
    "blues": "nhl", "blackhawks": "nhl", "red wings": "nhl", "wild": "nhl",
    "avalanche": "nhl", "avs": "nhl", "oilers": "nhl", "flames": "nhl",
    "canucks": "nhl", "jets": "nhl", "predators": "nhl", "preds": "nhl",
    "stars": "nhl", "ducks": "nhl", "sharks": "nhl", "golden knights": "nhl",
    "kraken": "nhl", "senators": "nhl", "sabres": "nhl", "coyotes": "nhl",
    "blue jackets": "nhl",
    # MLB
    "yankees": "mlb", "red sox": "mlb", "dodgers": "mlb", "cubs": "mlb",
    "astros": "mlb", "braves": "mlb", "mets": "mlb", "phillies": "mlb",
    "mariners": "mlb", "padres": "mlb", "angels": "mlb", "athletics": "mlb",
    "tigers": "mlb", "royals": "mlb", "white sox": "mlb", "twins": "mlb",
    "brewers": "mlb", "reds": "mlb", "pirates": "mlb", "nationals": "mlb",
    "marlins": "mlb", "orioles": "mlb", "rays": "mlb", "blue jays": "mlb",
    "rockies": "mlb", "diamondbacks": "mlb",
    # Premier League
    "arsenal": "epl", "chelsea": "epl", "liverpool": "epl",
    "tottenham": "epl", "everton": "epl", "leicester": "epl",
    "west ham": "epl", "newcastle": "epl", "aston villa": "epl",
    "man city": "epl", "man united": "epl", "man utd": "epl",
    "manchester city": "epl", "manchester united": "epl",
    "wolves": "epl", "crystal palace": "epl", "brentford": "epl",
    "brighton": "epl", "southampton": "epl", "nottingham": "epl",
    "fulham": "epl", "burnley": "epl",
    # La Liga
    "real madrid": "laliga", "barcelona": "laliga", "atletico madrid": "laliga",
    "sevilla": "laliga", "valencia": "laliga", "villarreal": "laliga",
    "real sociedad": "laliga", "athletic bilbao": "laliga", "betis": "laliga",
    # Bundesliga
    "bayern munich": "bundesliga", "borussia dortmund": "bundesliga",
    "bvb": "bundesliga", "rb leipzig": "bundesliga", "leverkusen": "bundesliga",
    "eintracht frankfurt": "bundesliga", "wolfsburg": "bundesliga",
    # Serie A
    "juventus": "seriea", "ac milan": "seriea", "inter milan": "seriea",
    "napoli": "seriea", "roma": "seriea", "lazio": "seriea",
    "atalanta": "seriea", "fiorentina": "seriea",
    # Ligue 1
    "psg": "ligue1", "paris saint-germain": "ligue1", "marseille": "ligue1",
    "lyon": "ligue1", "monaco": "ligue1",
    # MLS
    "inter miami": "mls", "la galaxy": "mls", "lafc": "mls",
    "seattle sounders": "mls", "portland timbers": "mls",
    "atlanta united": "mls", "nycfc": "mls", "toronto fc": "mls",
}

_ALL_SPORT_KEYS = ["nba", "nfl", "nhl", "mlb", "mls"]  # fetch all when sport unknown


def _detect_sport(question: str, category: str) -> str:
    """Return an ESPN sport key from category string or market question keywords."""
    cat = category.lower().strip()
    key = _SPORT_ALIASES.get(cat, "")
    if key:
        return key
    # Category is generic ("sports", "") — scan question
    q = question.lower()
    # Check explicit league abbreviations first
    for kw in ["nba", "nfl", "nhl", "mlb", "mls", "bundesliga", "ncaab", "ncaaf", "cfb"]:
        if kw in q:
            return _SPORT_ALIASES.get(kw, kw)
    # Fall back to team nickname lookup
    for nickname, sport in _TEAM_SPORT.items():
        if nickname in q:
            return sport
    return ""


def _team_matches(extracted: str, espn_name: str, espn_abbr: str) -> bool:
    """True if `extracted` is a reasonable match for an ESPN team."""
    ex = extracted.lower().strip()
    nm = espn_name.lower()
    ab = espn_abbr.lower()
    if ex == ab or ex in nm or nm.endswith(" " + ex):
        return True
    # city/nickname split: "Orlando Magic" → "magic" matches "Magic"
    words = nm.split()
    return bool(words) and (ex == words[-1] or ex == words[0])


async def _fetch_espn(sport_key: str) -> list[dict]:
    """Fetch today's scoreboard from ESPN. Returns parsed game dicts."""
    url = _ESPN_URLS.get(sport_key)
    if not url:
        return []
    now = time.time()
    cached = _SCORE_CACHE.get(sport_key)
    if cached and now - cached["ts"] < _SCORE_CACHE_TTL:
        return cached["events"]

    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=6.0) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            raw = r.json()
    except Exception as exc:
        logger.debug(f"ESPN {sport_key} fetch failed: {exc}")
        return cached["events"] if cached else []

    events: list[dict] = []
    for ev in raw.get("events", []):
        comps = ev.get("competitions", [])
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue
        status = ev.get("status", {})
        stype = status.get("type", {})
        teams = []
        for c in competitors:
            tm = c.get("team", {})
            teams.append({
                "name": tm.get("displayName", ""),
                "abbr": tm.get("abbreviation", ""),
                "score": c.get("score", "0"),
                "home_away": c.get("homeAway", "away"),
            })
        events.append({
            "id": ev.get("id", ""),
            "name": ev.get("name", ""),
            "short_name": ev.get("shortName", ""),
            "date": ev.get("date", ""),
            "state": stype.get("state", "pre"),   # pre / in / post
            "detail": stype.get("detail", ""),
            "short_detail": stype.get("shortDetail", ""),
            "period": status.get("period", 0),
            "clock": status.get("displayClock", ""),
            "teams": teams,
        })

    _SCORE_CACHE[sport_key] = {"ts": now, "events": events}
    return events

# Ring buffer for cross-exchange signal decisions (last 100)
_cross_exchange_log: collections.deque = collections.deque(maxlen=100)
_cross_exchange_log_lock = _threading.Lock()


def log_cross_exchange_decision(entry: dict) -> None:
    """Called by CrossExchangeStrategy to record each scan decision."""
    with _cross_exchange_log_lock:
        _cross_exchange_log.append({**entry, "ts": time.time()})


def register(portfolio, start_time: float, config=None, risk=None, binance=None, kalshi=None,
             news_monitor=None, hedge_manager=None,
             state_loaded_from_file: bool = False) -> None:
    global _portfolio, _bot_start_time, _config, _risk, _binance_ref, _kalshi_ref
    global _news_monitor_ref, _hedge_manager_ref, _state_loaded_from_file
    _portfolio = portfolio
    _bot_start_time = start_time
    _config = config
    _risk = risk
    _binance_ref = binance
    _kalshi_ref = kalshi
    _news_monitor_ref = news_monitor
    _hedge_manager_ref = hedge_manager
    _state_loaded_from_file = state_loaded_from_file


@app.post("/api/reset")
def reset_portfolio():
    """Reset the paper portfolio to starting balance.
    No auth required — this is a paper-trading-only endpoint.
    Historical trades are archived in SQLite and survive the reset so the
    meta-agent can learn from past performance across resets."""
    global _bot_start_time
    if not _portfolio:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=503)

    # Archive this reset event in the database so history is preserved
    try:
        from src.utils.database import _get_conn as _db_conn
        with _db_conn() as _conn:
            _conn.execute(
                """CREATE TABLE IF NOT EXISTS portfolio_resets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    trade_count INTEGER,
                    final_pnl REAL,
                    notes TEXT
                )"""
            )
            _conn.execute(
                "INSERT INTO portfolio_resets (timestamp, trade_count, final_pnl, notes) VALUES (?,?,?,?)",
                (time.time(), len(_portfolio.trades), round(_portfolio.total_pnl(
                    getattr(_portfolio, "last_mtm_prices", None) or None
                ), 2),
                 "manual reset via dashboard"),
            )
    except Exception as _e:
        logger.warning(f"Failed to archive reset record: {_e}")

    # Clear in-memory state (SQLite trades table is untouched — history survives)
    starting = _portfolio.starting_balance
    _portfolio.usdc_balance = starting
    _portfolio.positions.clear()
    _portfolio.trades.clear()
    _portfolio.open_orders.clear()
    _portfolio.closed_positions.clear()
    _portfolio._trade_counter = 0
    _portfolio.pnl_history = [{"t": time.time(), "value": starting, "pnl": 0.0}]
    if hasattr(_portfolio, "last_mtm_prices"):
        _portfolio.last_mtm_prices = {}
    _bot_start_time = time.time()

    # Clear hard stop so the bot can trade again after reset
    if _risk:
        _risk.reset_permanent_lock()

    _state_path = _os.getenv("STATE_FILE_PATH", "logs/portfolio_state.json")
    _portfolio.save_to_json(_state_path)
    return {"ok": True, "starting_balance": starting}


@app.post("/api/add-funds")
def add_funds(amount: float = 10000.0):
    """Add virtual USDC to the paper portfolio without resetting trades.
    Also raises starting_balance so PnL % stays meaningful."""
    if not _portfolio:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=503)
    amount = max(0.0, min(amount, 1_000_000.0))
    with _portfolio_lock:
        _portfolio.usdc_balance += amount
        _portfolio.starting_balance += amount
    _state_path = _os.getenv("STATE_FILE_PATH", "logs/portfolio_state.json")
    _portfolio.save_to_json(_state_path)
    logger.info(f"[PAPER] Added ${amount:,.0f} virtual USDC — new balance ${_portfolio.usdc_balance:,.2f}")
    return {"ok": True, "added": amount, "usdc_balance": round(_portfolio.usdc_balance, 2)}


# ------------------------------------------------------------------ #
#  Bot API endpoints                                                   #
# ------------------------------------------------------------------ #

@app.get("/api/bot_info")
def bot_info():
    return {"name": _bot_name, "peer_url": _peer_bot_url}


@app.get("/api/status")
def status():
    if not _portfolio:
        return {"status": "starting"}
    uptime = int(time.time() - _bot_start_time)
    with _portfolio_lock:
        p = _portfolio
        _mtm = getattr(p, "last_mtm_prices", None) or None
        total_pnl = round(p.total_pnl(_mtm), 2)
        realized = round(p.realized_closed_pnl(), 2)
        n_trades = len(p.trades)
        balance = round(p.usdc_balance, 2)
        starting_balance = round(p.starting_balance, 2)
        total_value = round(p.total_value(_mtm), 2)
        open_positions = len(p.positions)
        closed_positions = len(p.closed_positions)
        exposure = round(p.exposure(), 2)
        fees_paid = round(p.total_fees_paid(), 2)
        win_rate = p.win_rate()
        mtm_count = len(getattr(p, "last_mtm_prices", {}) or {})
    trades_per_hour = round(n_trades / max(uptime / 3600, 0.01), 1)
    fresh_start = (n_trades == 0 and uptime < 300)
    return {
        "status": "running",
        "paper_trading": _config.paper_trading if _config else True,
        "polymarket_address": (_config.polymarket.funder_address if _config else ""),
        "mtm_positions_priced": mtm_count,
        "uptime_seconds": uptime,
        "uptime": _fmt_uptime(uptime),
        "cycle_count": _cycle_count,
        "balance": balance,
        "starting_balance": starting_balance,
        "total_value": total_value,
        "pnl": total_pnl,
        "pnl_pct": round((total_pnl / starting_balance) * 100, 3),
        "realized_pnl": realized,
        "realized_pnl_pct": round((realized / starting_balance) * 100, 3),
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "total_trades": n_trades,
        "exposure": exposure,
        "fees_paid": fees_paid,
        "win_rate": win_rate,
        "trades_per_hour": trades_per_hour,
        "fresh_start": fresh_start,
        "state_loaded_from_file": _state_loaded_from_file,
    }


@app.get("/api/pnl_history")
def pnl_history():
    if not _portfolio:
        return []
    with _portfolio_lock:
        snapshot = list(_portfolio.pnl_history)
    return snapshot


@app.get("/api/positions")
def positions():
    if not _portfolio:
        return []
    with _portfolio_lock:
        items = list(_portfolio.positions.items())
    with _market_status_lock:
        status_snapshot = dict(_market_status)
    result = []
    for tid, pos in items:
        ms = status_snapshot.get(tid, {})
        end_date = ms.get("end_date_iso") or getattr(pos, "end_date_iso", "")
        # market_active: True when Polymarket reports the market is still active.
        # Defaults True (unknown = assume still live) to avoid false "overdue" alerts.
        market_active = ms.get("active", True) if ms else True
        result.append({
            "token_id": tid[:16] + "...",
            "token_id_full": tid,
            "question": pos.market_question[:70],
            "outcome": pos.outcome,
            "contracts": round(pos.contracts, 4),
            "avg_cost": round(pos.avg_cost, 4),
            "cost_basis": round(pos.cost_basis, 2),
            "strategy": pos.strategy,
            "opened_at": int(pos.opened_at),
            "end_date_iso": end_date,
            "market_active": market_active,
        })
    return result


@app.post("/api/positions/close")
def close_position(idx: int = 0, price: float = 0.001):
    """Force-close an open position by index at the given price (paper mode only)."""
    try:
        if price <= 0 or price > 1.0:
            return JSONResponse({"error": f"Invalid price {price}: must be between 0 (exclusive) and 1.0"}, status_code=400)
        if not _portfolio:
            return JSONResponse({"error": "Portfolio not initialized"}, status_code=503)

        with _portfolio_lock:
            keys = list(_portfolio.positions.keys())
            if idx < 0 or idx >= len(keys):
                return JSONResponse(
                    {"error": f"No position at index {idx} (have {len(keys)})"},
                    status_code=404,
                )
            matched_id = keys[idx]
            pos = _portfolio.positions[matched_id]
            contracts = pos.contracts
            strategy = pos.strategy
            trade = _portfolio.sell(
                token_id=matched_id,
                contracts=contracts,
                price=price,
                strategy=strategy,
                notes="Manual close via dashboard",
            )

        if not trade:
            return JSONResponse({"error": "sell() returned None — position may already be gone"}, status_code=500)

        try:
            _portfolio.save_to_json()
        except Exception as save_err:
            pass  # non-fatal: state will be saved on next auto-save cycle

        return {"ok": True, "token_id": matched_id[:16] or "(empty)", "contracts": round(contracts, 4), "price": price}
    except Exception as exc:
        return JSONResponse({"error": f"Unexpected error: {exc}"}, status_code=500)


@app.get("/api/closed_positions")
def closed_positions(limit: int = 100):
    limit = min(limit, 1000)
    if not _portfolio:
        return []
    with _portfolio_lock:
        recent = list(reversed(_portfolio.closed_positions))[:limit]
    return recent


@app.get("/api/trades")
def trades(limit: int = 100):
    limit = min(limit, 1000)
    if not _portfolio:
        return []
    with _portfolio_lock:
        recent = list(reversed(_portfolio.trades))[:limit]
    return [
        {
            "trade_id": t.trade_id,
            "strategy": t.strategy,
            "side": t.side,
            "token_id": (t.token_id[:12] + "…") if t.token_id and len(t.token_id) > 16 else (t.token_id or ""),
            "token_id_full": t.token_id or "",
            "contracts": round(t.contracts, 4),
            "price": round(t.price, 4),
            "usdc_amount": round(t.usdc_amount, 2),
            "fee": round(t.fee, 4),
            "timestamp": int(t.timestamp),
            "notes": t.notes[:80],
            "realized_pnl": round(getattr(t, "realized_pnl", 0.0), 4),
        }
        for t in recent
    ]


@app.get("/api/strategy_pnl")
def strategy_pnl():
    if not _portfolio:
        return {}
    with _portfolio_lock:
        pnl_map = _portfolio.strategy_pnl()
    return {k: round(v, 2) for k, v in pnl_map.items()}


@app.get("/api/strategy_trades")
def strategy_trades():
    """Trade counts per strategy over time buckets."""
    if not _portfolio:
        return {}
    with _portfolio_lock:
        trades_snapshot = list(_portfolio.trades)
    counts: dict[str, int] = {}
    for t in trades_snapshot:
        counts[t.strategy] = counts.get(t.strategy, 0) + 1
    return counts


@app.get("/api/logs")
def logs(since: float = 0, limit: int = 200):
    limit = min(limit, 1000)
    from src.utils.logger import get_log_buffer
    all_logs = get_log_buffer()
    filtered = [l for l in all_logs if l["t"] > since]
    return filtered[-limit:]


@app.get("/api/logs/stream")
async def logs_stream():
    """SSE stream of log lines."""
    from src.utils.logger import get_log_buffer
    async def generator():
        last_count = 0
        while True:
            buf = get_log_buffer()
            if len(buf) > last_count:
                for entry in buf[last_count:]:
                    yield {"data": json.dumps(entry)}
                last_count = len(buf)
            await asyncio.sleep(0.5)
    return EventSourceResponse(generator())


# ------------------------------------------------------------------ #
#  System & Analytics endpoints                                        #
# ------------------------------------------------------------------ #

@app.get("/api/system")
def system_status():
    """Return system connection status, strategy states, risk health, API keys, disk usage."""
    # --- Mode ---
    mode = "PAPER"
    if _config is not None:
        try:
            mode = "LIVE" if not _config.paper_trading else "PAPER"
        except Exception:
            pass

    # --- Strategies ---
    strategy_notes = {
        "combinatorial":     "Multi-outcome portfolio imbalance",
        "latency_arb":       "Polymarket lagging Binance prices",
        "market_making":     "Passive liquidity / earn spread",
        "resolution":        "Mispriced near-expiry markets",
        "event_driven":      "News/event catalyst markets",
        "cross_exchange":    "Polymarket vs Kalshi divergence",
        "futures_hedge":     "Binance futures hedge on crypto",
        "quick_resolution":  "High-conviction near-expiry entries",
        "crypto_5m":         "5m/15m dual-arb + Grok snipe",
        "swarm":             "MiroFish crowd simulation mispricing",
    }
    strategies = {}
    if _config is not None:
        try:
            cfg_s = _config.strategies
            for name, note in strategy_notes.items():
                attr = f"{name}_enabled"
                enabled = bool(getattr(cfg_s, attr, False))
                if not enabled and name == "latency_arb":
                    note = "Disabled — dynamic fees killed edge"
                strategies[name] = {"enabled": enabled, "note": note}
        except Exception:
            pass
    if not strategies:
        for name, note in strategy_notes.items():
            strategies[name] = {"enabled": False, "note": note}

    # --- Connections ---
    # Polymarket: just check if _portfolio is registered (proxy for connectivity)
    poly_ok = _portfolio is not None
    connections = {
        "polymarket": {
            "status": "ok" if poly_ok else "warn",
            "detail": "REST + WS active" if poly_ok else "Not yet connected",
        },
        "binance": {"status": "error", "detail": "Not configured"},
        "kalshi": {"status": "error", "detail": "Not configured"},
    }
    if _binance_ref is not None:
        try:
            if callable(getattr(_binance_ref, "is_connected", None)):
                connected = _binance_ref.is_connected()
            else:
                connected = True
            connections["binance"] = {
                "status": "ok" if connected else "warn",
                "detail": "WebSocket connected" if connected else "Reconnecting...",
            }
        except Exception:
            connections["binance"] = {"status": "warn", "detail": "Status unknown"}
    if _kalshi_ref is not None:
        try:
            if callable(getattr(_kalshi_ref, "_has_credentials", None)):
                creds = _kalshi_ref._has_credentials()
            else:
                creds = True
            err = getattr(_kalshi_ref, "_last_error", None)
            if not creds:
                kalshi_detail = (
                    "No credentials — use KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY "
                    "(recommended), KALSHI_API_TOKEN, or KALSHI_EMAIL + KALSHI_PASSWORD"
                )
                kalshi_status_str = "error"
            elif err:
                kalshi_detail = err[:200] + ("…" if len(err) > 200 else "")
                kalshi_status_str = "warn"
            else:
                kalshi_detail = "Connected — last /markets fetch succeeded"
                kalshi_status_str = "ok"
            connections["kalshi"] = {
                "status": kalshi_status_str,
                "detail": kalshi_detail,
            }
        except Exception:
            connections["kalshi"] = {"status": "warn", "detail": "Status unknown"}

    # --- API keys (True/False only, never expose values) ---
    api_keys = {
        "anthropic":    bool(os.getenv("ANTHROPIC_API_KEY")),
        "polymarket":   bool(os.getenv("POLYMARKET_API_KEY") or os.getenv("POLY_API_KEY")),
        "kalshi_rsa":   bool(os.getenv("KALSHI_RSA_KEY") or os.getenv("KALSHI_PRIVATE_KEY")),
        "kalshi_token": bool(os.getenv("KALSHI_API_TOKEN") or os.getenv("KALSHI_TOKEN")),
        "perplexity":   bool(os.getenv("PERPLEXITY_API_KEY")),
        "grok":         bool(os.getenv("GROK_API_KEY")),
    }

    # --- AI Research integrations ---
    ai_research = {
        "perplexity": {
            "configured": bool(os.getenv("PERPLEXITY_API_KEY")),
            "label": "Perplexity Sonar",
            "note": "Real-time web search — news polling, event context, meta-agent",
            "add_key": "PERPLEXITY_API_KEY",
            "docs": "https://docs.perplexity.ai",
        },
        "grok": {
            "configured": bool(os.getenv("GROK_API_KEY")),
            "label": "Grok (xAI)",
            "note": "Live X/Twitter sentiment — crypto snipe gate",
            "add_key": "GROK_API_KEY",
            "docs": "https://docs.x.ai/api",
        },
        "mirofish": {
            "configured": bool(os.getenv("MIROFISH_URL")),
            "label": "MiroFish",
            "note": "Full swarm simulation (1M agents) — fallback: LLM persona simulation",
            "add_key": "MIROFISH_URL",
            "docs": "https://github.com/666ghj/MiroFish",
        },
    }

    # --- Risk health ---
    risk_data = {
        "health_score": None,
        "health_grade": "N/A",
        "hard_stop": False,
        "drawdown_pct": 0.0,
        "exposure_pct": 0.0,
        "flags": [],
    }
    if _risk is not None:
        try:
            h = _risk.portfolio_health_score()
            risk_data["health_score"] = h.get("score")
            risk_data["health_grade"] = h.get("grade", "N/A")
            risk_data["hard_stop"] = h.get("hard_stop", False)
            risk_data["drawdown_pct"] = h.get("drawdown_pct", 0.0)
            risk_data["exposure_pct"] = h.get("exposure_pct", 0.0)
            risk_data["flags"] = h.get("flags", [])
        except Exception:
            pass

    # --- Meta-agent info ---
    meta_agent = {"enabled": False, "interval_minutes": 30, "last_run_ago_minutes": None}
    meta_agent["enabled"] = bool(os.getenv("ANTHROPIC_API_KEY"))
    try:
        meta_agent["interval_minutes"] = int(os.getenv("META_AGENT_INTERVAL_MINUTES", "30"))
    except Exception:
        pass
    meta_files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)
    if meta_files:
        try:
            mtime = os.path.getmtime(meta_files[0])
            meta_agent["last_run_ago_minutes"] = round((time.time() - mtime) / 60, 1)
        except Exception:
            pass

    # --- Disk usage ---
    log_files = glob.glob("logs/*.log") + glob.glob("logs/meta_agent_*.json")
    total_bytes = 0
    for lf in log_files:
        try:
            total_bytes += os.path.getsize(lf)
        except OSError:
            pass
    disk = {
        "log_files_count": len(log_files),
        "log_files_mb": round(total_bytes / (1024 * 1024), 2),
    }

    return {
        "mode": mode,
        "strategies": strategies,
        "connections": connections,
        "api_keys": api_keys,
        "ai_research": ai_research,
        "risk": risk_data,
        "meta_agent": meta_agent,
        "disk": disk,
    }


@app.get("/api/kalshi/status")
def kalshi_status():
    """Detailed Kalshi connection diagnostics and recent cross-exchange decisions."""
    has_email    = bool(os.getenv("KALSHI_EMAIL"))
    has_password = bool(os.getenv("KALSHI_PASSWORD"))
    has_token    = bool(os.getenv("KALSHI_API_TOKEN"))
    has_key_id   = bool(os.getenv("KALSHI_API_KEY_ID"))
    has_priv_key = bool(os.getenv("KALSHI_PRIVATE_KEY"))
    kalshi_enabled      = os.getenv("KALSHI_ENABLED", "false").lower() in ("true", "1", "yes")
    cross_ex_enabled    = os.getenv("STRATEGY_CROSS_EXCHANGE", "false").lower() in ("true", "1", "yes")
    safe_only           = os.getenv("CROSS_EXCHANGE_SAFE_ONLY", "true").lower() in ("true", "1", "yes")
    min_edge            = float(os.getenv("CROSS_EXCHANGE_MIN_EDGE", "0.05"))

    # Determine auth method and status
    if has_key_id and has_priv_key:
        auth_method = "RSA Key (recommended)"
        auth_ok = True
    elif has_token:
        auth_method = "Bearer Token"
        auth_ok = True
    elif has_email and has_password:
        auth_method = "Email + Password"
        auth_ok = True
    elif has_email and not has_password:
        auth_method = "Email set but KALSHI_PASSWORD missing"
        auth_ok = False
    elif has_key_id and not has_priv_key:
        auth_method = "KALSHI_API_KEY_ID set but KALSHI_PRIVATE_KEY missing"
        auth_ok = False
    else:
        auth_method = "No credentials"
        auth_ok = False

    # Checklist items with pass/fail and fix instructions
    checklist = [
        {
            "label": "KALSHI_ENABLED=true",
            "ok": kalshi_enabled,
            "fix": "Add KALSHI_ENABLED=true to Railway env vars",
        },
        {
            "label": "STRATEGY_CROSS_EXCHANGE=true",
            "ok": cross_ex_enabled,
            "fix": "Add STRATEGY_CROSS_EXCHANGE=true to Railway env vars",
        },
        {
            "label": f"Auth credentials ({auth_method})",
            "ok": auth_ok,
            "fix": (
                "Add one of: (A) KALSHI_EMAIL + KALSHI_PASSWORD  "
                "(B) KALSHI_API_TOKEN  "
                "(C) KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY  "
                "— get API keys at kalshi.com → Account → API Access"
            ),
        },
    ]

    # Runtime state from the live client reference
    runtime = {"client_loaded": False, "markets_cached": 0, "last_cache_age_s": None, "last_error": None}
    if _kalshi_ref is not None:
        runtime["client_loaded"] = True
        try:
            runtime["markets_cached"] = len(_kalshi_ref._market_cache)
            age = time.time() - _kalshi_ref._cache_ts if _kalshi_ref._cache_ts else None
            runtime["last_cache_age_s"] = round(age, 1) if age else None
            runtime["last_error"] = getattr(_kalshi_ref, "_last_error", None)
        except Exception:
            pass

    # Recent cross-exchange decisions (deque doesn't support slicing — convert first)
    with _cross_exchange_log_lock:
        recent = list(reversed(list(_cross_exchange_log)[-20:]))

    all_ok = all(c["ok"] for c in checklist)
    return {
        "overall_status": "active" if all_ok and runtime["client_loaded"] else "misconfigured",
        "auth_method": auth_method,
        "auth_ok": auth_ok,
        "kalshi_enabled": kalshi_enabled,
        "cross_exchange_enabled": cross_ex_enabled,
        "safe_only": safe_only,
        "min_edge_pct": round(min_edge * 100, 1),
        "checklist": checklist,
        "runtime": runtime,
        "recent_decisions": recent,
    }


@app.get("/api/kalshi/decisions")
def kalshi_decisions():
    """Return the last 100 cross-exchange scan decisions."""
    with _cross_exchange_log_lock:
        return list(reversed(_cross_exchange_log))


@app.get("/api/analytics")
def analytics():
    """Return strategy analytics, hourly PnL, health history, LLM decisions, edge distribution."""
    with _portfolio_lock:
        p = _portfolio
        if p is not None:
            _trades = list(p.trades)
            _closed = list(p.closed_positions)
            _pnl_history = list(p.pnl_history)
        else:
            _trades = []
            _closed = []
            _pnl_history = []

    # --- Strategy ROI, win rates, trade counts, volumes, fees ---
    strategy_roi: dict[str, float] = {}
    strategy_win_rates: dict[str, float] = {}
    strategy_trade_counts: dict[str, int] = {}
    strategy_volumes: dict[str, float] = {}
    strategy_fees: dict[str, float] = {}

    if p is not None:
        # Trade counts, volumes, fees from trades list
        vol_map: dict[str, float] = {}
        fee_map: dict[str, float] = {}
        count_map: dict[str, int] = {}
        for t in _trades:
            s = t.strategy
            count_map[s] = count_map.get(s, 0) + 1
            vol_map[s] = vol_map.get(s, 0.0) + t.usdc_amount
            fee_map[s] = fee_map.get(s, 0.0) + t.fee
        strategy_trade_counts = count_map
        strategy_volumes = {k: round(v, 2) for k, v in vol_map.items()}
        strategy_fees = {k: round(v, 4) for k, v in fee_map.items()}

        # PnL per strategy
        strat_pnl = {}
        try:
            with _portfolio_lock:
                strat_pnl = p.strategy_pnl()
        except Exception:
            pass

        # ROI = pnl / volume
        for s, vol in vol_map.items():
            pnl_val = strat_pnl.get(s, 0.0)
            if vol > 0:
                strategy_roi[s] = round((pnl_val / vol) * 100, 3)
            else:
                strategy_roi[s] = 0.0

        # Win rates from closed_positions
        wins_map: dict[str, int] = {}
        total_map: dict[str, int] = {}
        for cp in _closed:
            s = getattr(cp, "strategy", None) or cp.get("strategy", "") if isinstance(cp, dict) else getattr(cp, "strategy", "")
            rp = cp.get("realized_pnl", 0) if isinstance(cp, dict) else getattr(cp, "realized_pnl", 0)
            total_map[s] = total_map.get(s, 0) + 1
            if rp > 0:
                wins_map[s] = wins_map.get(s, 0) + 1
        for s, total in total_map.items():
            strategy_win_rates[s] = round(wins_map.get(s, 0) / total * 100, 1) if total > 0 else 0.0

    # --- Hourly PnL (last 24h) ---
    hourly_pnl: list[dict] = []
    if p is not None and _pnl_history:
        now = time.time()
        cutoff = now - 86400
        # bucket by hour
        buckets: dict[int, list[float]] = {}
        for point in _pnl_history:
            t_val = point.get("t", 0)
            if t_val < cutoff:
                continue
            hour_bucket = int(t_val // 3600)
            buckets.setdefault(hour_bucket, []).append(point.get("pnl", 0.0))

        if buckets:
            sorted_hours = sorted(buckets.keys())
            prev_pnl = 0.0
            for hb in sorted_hours:
                last_pnl = buckets[hb][-1]
                delta = last_pnl - prev_pnl
                import datetime
                label = datetime.datetime.fromtimestamp(hb * 3600).strftime("%H:00")
                hourly_pnl.append({"hour_label": label, "pnl": round(delta, 4)})
                prev_pnl = last_pnl

    # --- Health history from meta_agent_*.json ---
    health_history: list[dict] = []
    meta_files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)[:20]
    for mf in reversed(meta_files):
        try:
            with open(mf) as fp:
                data = json.load(fp)
            h = data.get("health", {})
            score = h.get("score") or h.get("health_score")
            grade = h.get("grade") or h.get("health_grade", "?")
            ts_val = data.get("timestamp", 0)
            if score is not None:
                health_history.append({"t": ts_val, "score": score, "grade": grade})
        except Exception:
            pass

    # --- LLM decisions (trades with [LLM] in notes) ---
    llm_decisions: list[dict] = []
    if p is not None:
        for t in reversed(_trades):
            notes = t.notes if isinstance(t.notes, str) else ""
            if "[LLM]" in notes:
                llm_decisions.append({
                    "trade_id": t.trade_id,
                    "strategy": t.strategy,
                    "side": t.side,
                    "price": round(t.price, 4),
                    "usdc_amount": round(t.usdc_amount, 2),
                    "timestamp": int(t.timestamp),
                    "notes": notes[:120],
                })
                if len(llm_decisions) >= 20:
                    break

    # --- Edge distribution (bucket edges seen in trade notes) ---
    edge_distribution: dict[str, int] = {
        "0-1%": 0, "1-2%": 0, "2-3%": 0, "3-5%": 0, "5-10%": 0, "10%+": 0
    }
    if p is not None:
        import re as _re
        for t in _trades:
            notes = t.notes if isinstance(t.notes, str) else ""
            m = _re.search(r"edge[=:]\s*([\d.]+)", notes, _re.IGNORECASE)
            if m:
                try:
                    edge_pct = float(m.group(1)) * 100
                    if edge_pct < 1:
                        edge_distribution["0-1%"] += 1
                    elif edge_pct < 2:
                        edge_distribution["1-2%"] += 1
                    elif edge_pct < 3:
                        edge_distribution["2-3%"] += 1
                    elif edge_pct < 5:
                        edge_distribution["3-5%"] += 1
                    elif edge_pct < 10:
                        edge_distribution["5-10%"] += 1
                    else:
                        edge_distribution["10%+"] += 1
                except ValueError:
                    pass

    return {
        "strategy_roi": strategy_roi,
        "strategy_win_rates": strategy_win_rates,
        "strategy_trade_counts": strategy_trade_counts,
        "strategy_volumes": strategy_volumes,
        "strategy_fees": strategy_fees,
        "hourly_pnl": hourly_pnl,
        "health_history": health_history,
        "llm_decisions": llm_decisions,
        "edge_distribution": edge_distribution,
    }


# ------------------------------------------------------------------ #
#  Balances endpoint                                                   #
# ------------------------------------------------------------------ #

@app.get("/api/balances")
async def balances():
    """Estimated spend on Anthropic, Railway disk usage, billing cycle info."""
    import datetime

    now = time.time()
    today = datetime.date.today()

    # Billing cycle: 1st of this month → 1st of next month
    cycle_start = datetime.date(today.year, today.month, 1)
    if today.month == 12:
        cycle_end = datetime.date(today.year + 1, 1, 1)
    else:
        cycle_end = datetime.date(today.year, today.month + 1, 1)
    days_in_cycle = (cycle_end - cycle_start).days
    days_elapsed = max((today - cycle_start).days, 0)
    days_remaining = max((cycle_end - today).days, 0)
    cycle_pct = round(days_elapsed / days_in_cycle * 100, 1) if days_in_cycle else 0

    # --- Anthropic cost estimate ---
    # Each meta-agent run uses Claude Opus 4.6 with extended thinking.
    # ~2500 input tokens  @ $15/MTok = $0.0375
    # ~10000 output+think @ $75/MTok = $0.75
    # ≈ $0.79/run (conservative estimate)
    meta_files = sorted(glob.glob("logs/meta_agent_*.json"))
    meta_run_count = len(meta_files)
    COST_PER_RUN = 0.79
    estimated_anthropic_cost = round(meta_run_count * COST_PER_RUN, 2)
    daily_runs = meta_run_count / max(days_elapsed, 1)
    projected_monthly = round(daily_runs * days_in_cycle * COST_PER_RUN, 2)
    try:
        anthropic_budget: float | None = float(os.getenv("ANTHROPIC_MONTHLY_BUDGET") or 0) or None
    except Exception:
        anthropic_budget = None

    # --- Railway disk usage (local filesystem) ---
    log_files = (
        glob.glob("logs/*.log")
        + glob.glob("logs/meta_agent_*.json")
        + glob.glob("logs/*.json")
    )
    total_bytes = sum(
        os.path.getsize(f) for f in log_files if os.path.exists(f)
    )
    disk_used_mb = round(total_bytes / (1024 * 1024), 2)
    try:
        vol_limit_mb = int(os.getenv("RAILWAY_VOLUME_LIMIT_MB") or 512)
    except Exception:
        vol_limit_mb = 512
    disk_pct = round(disk_used_mb / vol_limit_mb * 100, 1) if vol_limit_mb else 0

    try:
        railway_base = float(os.getenv("RAILWAY_PLAN_COST") or 5)
    except Exception:
        railway_base = 5.0

    # --- Bot summary ---
    uptime_hours = round((now - _bot_start_time) / 3600, 2) if _bot_start_time else 0
    with _portfolio_lock:
        bot_trades = len(_portfolio.trades) if _portfolio else 0
        _mtm = getattr(_portfolio, "last_mtm_prices", None) or None
        bot_pnl = round(_portfolio.total_pnl(_mtm), 2) if _portfolio else 0.0

    return {
        "billing_cycle": {
            "start": str(cycle_start),
            "end": str(cycle_end),
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "days_total": days_in_cycle,
            "cycle_pct": cycle_pct,
        },
        "anthropic": {
            "meta_agent_runs": meta_run_count,
            "cost_per_run_usd": COST_PER_RUN,
            "estimated_cost_usd": estimated_anthropic_cost,
            "projected_monthly_usd": projected_monthly,
            "monthly_budget_usd": anthropic_budget,
            "key_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
            "model": "claude-opus-4-6",
        },
        "railway": {
            "disk_used_mb": disk_used_mb,
            "disk_limit_mb": vol_limit_mb,
            "disk_pct": disk_pct,
            "plan_base_cost_usd": railway_base,
            "days_remaining_in_cycle": days_remaining,
        },
        "bot": {
            "uptime_hours": uptime_hours,
            "trades_executed": bot_trades,
            "total_pnl_usd": bot_pnl,
            "paper_trading": True,
        },
    }


# ------------------------------------------------------------------ #
#  Meta-agent API endpoints                                            #
# ------------------------------------------------------------------ #

@app.get("/api/meta/history")
def meta_history():
    files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)[:10]
    results = []
    for f in files:
        try:
            with open(f) as fp:
                data = json.load(fp)
            # Support both old format (portfolio_snapshot) and new format (portfolio_summary)
            old_snapshot = data.get("portfolio_snapshot", {})
            new_summary = data.get("portfolio_summary", {})
            portfolio_block = new_summary or old_snapshot.get("portfolio", {})
            portfolio_pnl = portfolio_block.get("total_pnl_usdc", 0)
            results.append({
                "file": os.path.basename(f),
                "timestamp": data.get("timestamp", 0),
                "proposed_changes": data.get("proposed_changes", {}),
                "applied_changes": data.get("applied_changes", []),
                "analysis_preview": data.get("analysis", "")[:300],
                "portfolio_pnl": portfolio_pnl,
                "health": data.get("health", {}),
                "strategy_roi_pct": data.get("strategy_roi_pct", {}),
            })
        except Exception:
            pass
    return results


@app.get("/api/meta/latest")
def meta_latest():
    files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)
    if not files:
        return {"found": False}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return {"found": True, **data}
    except Exception:
        return {"found": False}


# ------------------------------------------------------------------ #
#  Agent timers endpoint                                               #
# ------------------------------------------------------------------ #

@app.get("/api/agent_timers")
def agent_timers():
    """Return last-run timestamps + intervals so the dashboard can show countdowns."""
    now = time.time()

    # Meta-agent: runs every META_AGENT_INTERVAL_MINUTES (default 30)
    meta_interval = int(os.getenv("META_AGENT_INTERVAL_MINUTES", "30")) * 60
    meta_files = sorted(glob.glob("logs/meta_agent_[0-9]*.json"), reverse=True)
    meta_last = 0
    if meta_files:
        try:
            meta_last = os.path.getmtime(meta_files[0])
        except OSError:
            pass

    # Research: runs every RESEARCH_INTERVAL_HOURS (default 2)
    research_interval = float(os.getenv("RESEARCH_INTERVAL_HOURS", "2")) * 3600
    research_files = sorted(glob.glob("logs/research_*.json"), reverse=True)
    research_last = 0
    if research_files:
        try:
            research_last = os.path.getmtime(research_files[0])
        except OSError:
            pass

    # Code review: runs weekly (7 days)
    review_interval = 7 * 24 * 3600
    review_files = sorted(glob.glob("logs/code_review_*.json"), reverse=True)
    review_last = 0
    if review_files:
        try:
            review_last = os.path.getmtime(review_files[0])
        except OSError:
            pass

    def _next_in(last_ts, interval):
        if last_ts == 0:
            return None  # never run
        return max(0.0, last_ts + interval - now)

    return {
        "now": now,
        "meta_agent":    {"last_run": meta_last,     "interval_secs": meta_interval,     "next_in_secs": _next_in(meta_last, meta_interval)},
        "research":      {"last_run": research_last,  "interval_secs": research_interval,  "next_in_secs": _next_in(research_last, research_interval)},
        "code_review":   {"last_run": review_last,    "interval_secs": review_interval,    "next_in_secs": _next_in(review_last, review_interval)},
    }


# ------------------------------------------------------------------ #
#  Code Review API endpoints                                           #
# ------------------------------------------------------------------ #

@app.get("/api/research/latest")
def research_latest():
    files = sorted(glob.glob("logs/research_*.json"), reverse=True)
    if not files:
        return {"found": False}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return {"found": True, **data}
    except Exception:
        return {"found": False}


@app.get("/api/research/list")
def research_list():
    files = sorted(glob.glob("logs/research_*.json"), reverse=True)[:24]
    results = []
    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            results.append({
                "date": data.get("date", ""),
                "run_hour": data.get("run_hour", ""),
                "timestamp": data.get("timestamp", 0),
                "topics_searched": data.get("topics_searched", []),
                "finding_count": data.get("finding_count", 0),
                "high_count": data.get("high_count", 0),
                "web_search_used": data.get("web_search_used", False),
                "top_insights": data.get("top_insights", [])[:2],
            })
        except Exception:
            pass
    return results


_review_running: bool = False
_meta_agent_running: bool = False
_meta_agent_trigger: bool = False
_meta_agent_last_run_ts: float = 0.0
_meta_agent_last_error: str = ""


@app.post("/api/code_review/run_now")
async def code_review_run_now(x_api_key: str = Header(default="")):
    if not _check_api_key(x_api_key):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    global _review_running
    if _review_running:
        return JSONResponse({"ok": False, "error": "Review already running"}, status_code=409)
    _review_running = True

    async def _run():
        global _review_running
        try:
            from src.meta_agent.code_reviewer import run_code_review
            await run_code_review()
        except Exception as exc:
            import src.utils.logger as _log
            _log.logger.warning(f"Manual code review error: {exc}")
        finally:
            _review_running = False

    asyncio.create_task(_run())
    return {"ok": True}


@app.get("/api/code_review/run_now/status")
def code_review_run_now_status():
    return {"running": _review_running}


@app.post("/api/meta-agent/run-now")
async def meta_agent_run_now():
    global _meta_agent_trigger
    if _meta_agent_running:
        return JSONResponse({"ok": False, "error": "Already running"}, status_code=409)
    _meta_agent_trigger = True
    return {"ok": True}


@app.get("/api/meta-agent/run-now/status")
def meta_agent_run_now_status():
    heartbeat = {}
    try:
        with open("logs/meta_agent_heartbeat.json") as f:
            heartbeat = json.load(f)
    except Exception:
        pass
    return {
        "running": _meta_agent_running,
        "last_run_ts": _meta_agent_last_run_ts,
        "last_error": _meta_agent_last_error,
        "heartbeat": heartbeat,
    }


@app.get("/api/code_review/latest")
def code_review_latest():
    files = sorted(glob.glob("logs/code_review_*.json"), reverse=True)
    if not files:
        return {"found": False}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return {"found": True, **data}
    except Exception:
        return {"found": False}


@app.get("/api/code_review/list")
def code_review_list():
    files = sorted(glob.glob("logs/code_review_*.json"), reverse=True)[:5]
    results = []
    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            results.append({
                "date": data.get("date", ""),
                "timestamp": data.get("timestamp", 0),
                "grade": data.get("grade", "?"),
                "health_score": data.get("health_score"),
                "total_findings": data.get("total_findings", 0),
                "high_findings": data.get("high_findings", 0),
                "medium_findings": data.get("medium_findings", 0),
                "summary": data.get("summary", "")[:200],
            })
        except Exception:
            pass
    return results


# ------------------------------------------------------------------ #
#  Auto-fix endpoints                                                  #
# ------------------------------------------------------------------ #

_autofix_status: dict = {"state": "idle", "results": [], "error": None, "started_at": None, "finished_at": None, "git": None}
_pending_deploys: list[dict] = []
_pending_deploys_lock = _threading.Lock()


def get_pending_deploys() -> list[dict]:
    with _pending_deploys_lock:
        return list(_pending_deploys)


def clear_pending_deploys() -> None:
    with _pending_deploys_lock:
        _pending_deploys.clear()


async def _run_autofix_task() -> None:
    """Read the latest code review, then ask Claude to fix each high/medium finding."""
    global _autofix_status
    import ast as _ast
    import anthropic as _anthropic

    _autofix_status["results"] = []
    _autofix_status["error"] = None

    # Load latest review
    files = sorted(glob.glob("logs/code_review_*.json"), reverse=True)
    if not files:
        _autofix_status["state"] = "error"
        _autofix_status["error"] = "No code review found. Run the weekly review first."
        _autofix_status["finished_at"] = time.time()
        return

    try:
        with open(files[0]) as f:
            review = json.load(f)
    except Exception as e:
        _autofix_status["state"] = "error"
        _autofix_status["error"] = f"Failed to load review: {e}"
        _autofix_status["finished_at"] = time.time()
        return

    findings = [
        fi for fi in (review.get("findings") or [])
        if fi.get("severity") in ("high", "medium") and fi.get("file")
    ]

    if not findings:
        _autofix_status["state"] = "done"
        _autofix_status["results"] = [{"status": "info", "message": "No high/medium findings to fix."}]
        _autofix_status["finished_at"] = time.time()
        return

    client = _anthropic.AsyncAnthropic()

    for finding in findings:
        file_path = finding.get("file", "")
        result_entry = {
            "file": file_path,
            "title": finding.get("title", ""),
            "severity": finding.get("severity", ""),
            "status": "pending",
            "message": "",
        }
        _autofix_status["results"].append(result_entry)

        # Read source file
        abs_path = os.path.join(".", file_path)
        try:
            with open(abs_path, encoding="utf-8") as f:
                source = f.read()
        except Exception as e:
            result_entry["status"] = "skip"
            result_entry["message"] = f"Cannot read file: {e}"
            continue

        if len(source) > 12000:
            source = source[:12000] + "\n# ... (truncated)"

        prompt = f"""You are a code fixer. A code review found this issue in `{file_path}`:

**Title:** {finding.get('title', '')}
**Severity:** {finding.get('severity', '')}
**Category:** {finding.get('category', '')}
**Description:** {finding.get('description', '')}
**Suggestion:** {finding.get('suggestion', '')}

Here is the current file content:
```python
{source}
```

Respond with a JSON object (and nothing else) in this exact format:
{{
  "fixable": true,
  "old_string": "exact substring to replace (must be unique in the file)",
  "new_string": "replacement substring",
  "explanation": "one-line explanation of what was changed"
}}

If the issue is not programmatically fixable (e.g. architectural, already fixed, or requires context you lack), respond with:
{{
  "fixable": false,
  "explanation": "reason"
}}

Rules:
- old_string must appear EXACTLY once in the file
- Make the minimal change needed
- Do not add docstrings, comments, or extra formatting
- Do not rewrite the whole file"""

        try:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            # Use next() to skip non-text blocks (e.g. thinking blocks)
            raw = next((b.text for b in response.content if hasattr(b, "text")), "").strip()
            if not raw:
                raise ValueError("Empty response from Claude")
            # Extract JSON — strip markdown fences, find first { ... } block
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            # If there's prose before the JSON object, find the first {
            brace = raw.find("{")
            if brace > 0:
                raw = raw[brace:]
            # Truncate after the last } to strip trailing prose
            rbrace = raw.rfind("}")
            if rbrace >= 0:
                raw = raw[:rbrace + 1]
            fix = json.loads(raw)
        except Exception as e:
            result_entry["status"] = "error"
            result_entry["message"] = f"Claude error: {e}"
            continue

        if not fix.get("fixable"):
            result_entry["status"] = "skip"
            result_entry["message"] = fix.get("explanation", "Not fixable.")
            continue

        old_str = fix.get("old_string", "")
        new_str = fix.get("new_string", "")

        if not old_str or old_str not in source:
            result_entry["status"] = "skip"
            result_entry["message"] = "old_string not found in file (may already be fixed)."
            continue

        if source.count(old_str) > 1:
            result_entry["status"] = "skip"
            result_entry["message"] = "old_string is not unique — skipping to avoid incorrect edit."
            continue

        new_source = source.replace(old_str, new_str, 1)

        # Validate syntax if Python file
        if file_path.endswith(".py"):
            try:
                _ast.parse(new_source)
            except SyntaxError as e:
                result_entry["status"] = "error"
                result_entry["message"] = f"Syntax error after fix: {e}"
                continue

        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_source)
            result_entry["status"] = "fixed"
            result_entry["message"] = fix.get("explanation", "Fixed.")
        except Exception as e:
            result_entry["status"] = "error"
            result_entry["message"] = f"Write failed: {e}"

    # --- Commit and push any fixed files to GitHub so changes survive redeploys ---
    fixed_files = [r["file"] for r in _autofix_status["results"] if r["status"] == "fixed"]
    git_result = await _git_commit_push(fixed_files) if fixed_files else {"pushed": False, "message": "No files to commit."}
    _autofix_status["git"] = git_result
    _autofix_status["state"] = "done"
    _autofix_status["finished_at"] = time.time()


async def _git_commit_push(changed_files: list[str]) -> dict:
    """
    Commit changed files to GitHub using the REST API (no git CLI required).
    Uses GITHUB_TOKEN + GITHUB_REPO env vars.
    GITHUB_REPO defaults to 'MichaelParizhsky/polymarket-arb-bot'.
    Returns a dict with 'pushed' (bool) and 'message' (str).
    """
    import base64 as _b64
    import httpx as _httpx

    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return {
            "pushed": False,
            "message": "GITHUB_TOKEN not set — fixes written to disk only. "
                       "Add GITHUB_TOKEN to Railway env vars to enable auto-push.",
        }

    repo = os.getenv("GITHUB_REPO", "")
    if not repo:
        return {"pushed": False, "message": "GITHUB_REPO not set — add it to Railway env vars (e.g. username/polymarket-arb-bot)."}
    api = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    pushed_files = []
    errors = []

    async with _httpx.AsyncClient(timeout=30) as client:
        for rel_path in changed_files:
            abs_path = os.path.join(".", rel_path)
            try:
                with open(abs_path, "rb") as fh:
                    content_bytes = fh.read()
            except OSError as exc:
                errors.append(f"{rel_path}: read error — {exc}")
                continue

            encoded = _b64.b64encode(content_bytes).decode()

            # Get current file SHA (required by GitHub API to update a file)
            get_resp = await client.get(f"{api}/contents/{rel_path}", headers=headers)
            if get_resp.status_code == 200:
                current_sha = get_resp.json().get("sha", "")
            elif get_resp.status_code == 404:
                current_sha = ""  # new file
            else:
                errors.append(f"{rel_path}: GitHub GET failed ({get_resp.status_code})")
                continue

            commit_msg = f"autofix: fix {rel_path} via dashboard code review"
            payload: dict = {
                "message": commit_msg,
                "content": encoded,
                "branch": "main",
            }
            if current_sha:
                payload["sha"] = current_sha

            put_resp = await client.put(
                f"{api}/contents/{rel_path}", headers=headers, json=payload
            )
            if put_resp.status_code in (200, 201):
                pushed_files.append(rel_path)
            else:
                errors.append(f"{rel_path}: GitHub PUT failed ({put_resp.status_code}) — {put_resp.text[:200]}")

    if pushed_files:
        msg = f"Committed {len(pushed_files)} file(s) to GitHub → Railway redeploy triggered."
        if errors:
            msg += f" ({len(errors)} error(s): {'; '.join(errors)})"
        return {"pushed": True, "message": msg, "files": pushed_files}
    else:
        return {"pushed": False, "message": f"No files pushed. Errors: {'; '.join(errors) or 'none'}"}


@app.post("/api/code_review/autofix")
async def code_review_autofix(x_api_key: str = Header(default="")):
    if not _check_api_key(x_api_key):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    global _autofix_status
    if _autofix_status.get("state") == "running":
        return JSONResponse({"ok": False, "error": "Already running"}, status_code=409)
    _autofix_status = {
        "state": "running",
        "results": [],
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    asyncio.create_task(_run_autofix_task())
    return {"ok": True}


@app.get("/api/code_review/autofix/status")
def code_review_autofix_status():
    return _autofix_status


# ------------------------------------------------------------------ #
#  Research signals + strategy proposals                              #
# ------------------------------------------------------------------ #

@app.get("/api/research/signals")
def research_signals():
    try:
        with open("logs/research_signals.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"active_topics": [], "strategy_focus": None, "param_hints": {}}


@app.get("/api/research/proposals/list")
def research_proposals_list():
    import glob as _glob
    metas = sorted(_glob.glob("logs/proposals/*_meta.json"), reverse=True)[:20]
    results = []
    for mp in metas:
        try:
            with open(mp) as f:
                results.append(json.load(f))
        except Exception:
            pass
    return results


@app.get("/api/research/proposals/{proposal_id}")
def research_proposal_detail(proposal_id: str):
    import glob as _glob
    # Find meta file by id
    for mp in _glob.glob("logs/proposals/*_meta.json"):
        try:
            with open(mp) as f:
                meta = json.load(f)
            if meta.get("id") == proposal_id:
                # Load code
                code = ""
                try:
                    with open(meta["file_path"], encoding="utf-8") as f:
                        code = f.read()
                except Exception:
                    pass
                return {**meta, "code": code}
        except Exception:
            pass
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.post("/api/research/proposals/{proposal_id}/deploy")
async def deploy_proposal(proposal_id: str, x_api_key: str = Header(default="")):
    if not _check_api_key(x_api_key):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    import ast as _ast
    import glob as _glob
    import shutil as _shutil

    for mp in _glob.glob("logs/proposals/*_meta.json"):
        try:
            with open(mp) as f:
                meta = json.load(f)
        except Exception:
            continue
        if meta.get("id") != proposal_id:
            continue

        if meta.get("deployed"):
            return JSONResponse({"ok": False, "error": "Already deployed"}, status_code=409)

        # Path traversal guard: validate file_path is under logs/proposals/
        _proposals_dir = os.path.realpath("logs/proposals")
        _requested = os.path.realpath(meta.get("file_path", ""))
        if not _requested.startswith(_proposals_dir + os.sep):
            return JSONResponse({"ok": False, "error": "Invalid file path"}, status_code=400)

        # Path traversal guard: validate file_name has no directory components
        _file_name = meta.get("file_name", "")
        if not _file_name or os.sep in _file_name or "/" in _file_name or ".." in _file_name:
            return JSONResponse({"ok": False, "error": "Invalid file name"}, status_code=400)

        # Load and validate code
        try:
            with open(meta["file_path"], encoding="utf-8") as f:
                code = f.read()
            _ast.parse(code)
        except SyntaxError as exc:
            return JSONResponse({"ok": False, "error": f"Syntax error: {exc}"}, status_code=400)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        # Copy to src/strategies/
        dest = os.path.join("src", "strategies", meta["file_name"])
        try:
            _shutil.copy2(meta["file_path"], dest)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"Copy failed: {exc}"}, status_code=500)

        # Mark deployed
        meta["deployed"] = True
        meta["deployed_at"] = time.time()
        meta["deployed_path"] = dest
        with open(mp, "w") as f:
            json.dump(meta, f, indent=2)

        # Queue for hot-load by main bot loop (lock for cross-thread safety)
        with _pending_deploys_lock:
            _pending_deploys.append({
                "proposal_id": proposal_id,
                "class_name": meta["class_name"],
                "file_path": dest,
                "deployed_at": meta["deployed_at"],
            })

        return {"ok": True, "deployed_path": dest, "class_name": meta["class_name"]}

    return JSONResponse({"error": "Proposal not found"}, status_code=404)


# ------------------------------------------------------------------ #
#  AI Intel API endpoints                                              #
# ------------------------------------------------------------------ #

@app.get("/api/news")
async def api_news():
    if _news_monitor_ref is None:
        return {"headlines": [], "status": "unavailable"}
    try:
        items = list(_news_monitor_ref._cache)
        items.sort(key=lambda h: h.get("_ts", 0), reverse=True)
        clean = [
            {k: v for k, v in h.items() if not k.startswith("_")}
            for h in items[:20]
        ]
        return {"headlines": clean, "status": "ok"}
    except Exception as e:
        return {"headlines": [], "status": str(e)}


@app.get("/api/hedges")
async def api_hedges():
    if _hedge_manager_ref is None:
        return {"hedges": {}, "count": 0, "enabled": False}
    try:
        status = _hedge_manager_ref.get_status()
        return status
    except Exception as e:
        return {"hedges": {}, "count": 0, "enabled": False, "error": str(e)}


# ------------------------------------------------------------------ #
#  Compare endpoint                                                    #
# ------------------------------------------------------------------ #

@app.get("/api/compare")
async def api_compare():
    import httpx, time

    # Bot A data (local — mirrors /api/status fields exactly)
    bot_a = {"available": True, "name": _bot_name or "Bot A"}
    try:
        uptime = int(time.time() - _bot_start_time)
        with _portfolio_lock:
            p = _portfolio
            bal            = round(p.usdc_balance, 2) if p else 0
            _cmp_mtm = getattr(p, "last_mtm_prices", None) or None if p else None
            total_pnl      = round(p.total_pnl(_cmp_mtm), 2) if p else 0
            starting_bal   = round(p.starting_balance, 2) if p else 0
            pnl_pct        = round((total_pnl / starting_bal) * 100, 3) if starting_bal else 0
            pos            = len(p.positions) if p else 0
            closed_pos     = len(p.closed_positions) if p else 0
            n_trades       = len(p.trades) if p else 0
            exposure       = round(p.exposure(), 2) if p else 0
            fees_paid      = round(p.total_fees_paid(), 2) if p else 0
            win_rate       = p.win_rate() if p else 0
            realized_pnl   = round(p.realized_closed_pnl(), 2) if p else 0
            strat_pnl      = {k: round(float(v), 2) for k, v in p.strategy_pnl().items()} if p else {}
            strat_trades   = {}
            if p:
                for t in p.trades:
                    strat_trades[t.strategy] = strat_trades.get(t.strategy, 0) + 1
        trades_per_hour = round(n_trades / max(uptime / 3600, 0.01), 1)
        bot_a.update({
            "configured":       True,
            "balance":          bal,
            "starting_balance": starting_bal,
            "total_pnl":        total_pnl,
            "pnl_pct":          pnl_pct,
            "open_positions":   pos,
            "closed_positions": closed_pos,
            "total_trades":     n_trades,
            "exposure":         exposure,
            "fees_paid":        fees_paid,
            "win_rate":         win_rate,
            "realized_pnl":     realized_pnl,
            "trades_per_hour":  trades_per_hour,
            "strategy_pnl":     strat_pnl,
            "strategy_trades":  strat_trades,
            "fresh_start":      (n_trades == 0 and uptime < 300),
            "uptime_seconds":   uptime,
            "state_loaded_from_file": _state_loaded_from_file,
        })
    except Exception as e:
        bot_a["error"] = str(e)

    # Bot B data (remote)
    bot_b = {"available": False, "configured": False, "name": "Bot B"}
    peer_url = _peer_bot_url
    if peer_url:
        bot_b["configured"] = True
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r_status, r_pnl, r_strat_trades = await asyncio.gather(
                    client.get(f"{peer_url}/api/status"),
                    client.get(f"{peer_url}/api/strategy_pnl"),
                    client.get(f"{peer_url}/api/strategy_trades"),
                    return_exceptions=True,
                )
            if not isinstance(r_status, Exception) and r_status.status_code == 200:
                s = r_status.json()
                b_uptime = s.get("uptime_seconds", 0)
                b_trades = s.get("total_trades", 0)
                bot_b.update({
                    "available":             True,
                    # Use correct field names matching /api/status response
                    "balance":               s.get("balance", 0),
                    "starting_balance":      s.get("starting_balance", 0),
                    "total_pnl":             s.get("pnl", 0),
                    "pnl_pct":               s.get("pnl_pct", 0),
                    "open_positions":        s.get("open_positions", 0),
                    "closed_positions":      s.get("closed_positions", 0),
                    "total_trades":          b_trades,
                    "exposure":              s.get("exposure", 0),
                    "fees_paid":             s.get("fees_paid", 0),
                    "win_rate":              s.get("win_rate", 0),
                    "realized_pnl":          s.get("realized_pnl", 0),
                    "trades_per_hour":       s.get("trades_per_hour", 0),
                    "uptime_seconds":        b_uptime,
                    "fresh_start":           s.get("fresh_start", b_trades == 0 and b_uptime < 300),
                    "state_loaded_from_file": s.get("state_loaded_from_file", False),
                })
                # Grab bot name from peer if available
                bot_b["name"] = s.get("bot_name", "Bot B")
            if not isinstance(r_pnl, Exception) and r_pnl.status_code == 200:
                bot_b["strategy_pnl"] = r_pnl.json()
            if not isinstance(r_strat_trades, Exception) and r_strat_trades.status_code == 200:
                bot_b["strategy_trades"] = r_strat_trades.json()
        except Exception as e:
            bot_b["error"] = str(e)
    else:
        bot_b["configured"] = False
        bot_b["error"] = "PEER_BOT_URL not set"

    return {"bot_a": bot_a, "bot_b": bot_b, "ts": time.time()}


# ------------------------------------------------------------------ #
#  Weather stats endpoint                                              #
# ------------------------------------------------------------------ #

@app.get("/api/sports/stats")
def sports_stats(api_key: str = Depends(_check_api_key)):
    """Sports-specific trade analytics: per-bet-type breakdown, league breakdown, recent trades."""
    from src.sports.sports_intel import classify_market, _detect_league

    if not _portfolio:
        return {"total_trades": 0, "wins": 0, "win_rate": 0, "total_pnl": 0,
                "open_count": 0, "open_positions": [], "bet_type_breakdown": [],
                "league_breakdown": [], "recent_trades": []}

    with _portfolio_lock:
        trades = list(_portfolio.trades)
        positions = list(_portfolio.positions.values())
        closed = list(_portfolio.closed_positions)
        last_mtm = dict(getattr(_portfolio, "last_mtm_prices", {}) or {})

    # Classify each sports trade by bet type — use master keyword set
    def _is_sports(question: str) -> bool:
        return bool(_SPORTS_KW_MASTER.search(question))

    sports_trades = [
        t for t in trades
        if _is_sports(getattr(t, "notes", "") + " " + getattr(_portfolio.positions.get(t.token_id, None) if _portfolio else None or {}, "market_question", ""))
    ]
    # Also include trades linked to sports positions
    sports_pos_tokens = {
        pos.token_id
        for pos in positions
        if _is_sports(pos.market_question)
    }
    sports_closed = [
        p for p in closed
        if _is_sports(p.get("market_question", ""))
    ]

    # Per-bet-type stats from closed positions
    bet_type_stats: dict[str, dict] = {}
    league_stats: dict[str, dict] = {}

    for pos in sports_closed:
        q = pos.get("market_question", "")
        pnl = pos.get("realized_pnl", 0.0)
        won = pnl > 0

        clf = classify_market(q)
        bt = clf.bet_type
        league = clf.league or _detect_league(q) or "unknown"

        for key, stats_dict in [(bt, bet_type_stats), (league, league_stats)]:
            if key not in stats_dict:
                stats_dict[key] = {"trades": 0, "wins": 0, "pnl": 0.0}
            stats_dict[key]["trades"] += 1
            stats_dict[key]["wins"] += int(won)
            stats_dict[key]["pnl"] += pnl

    # Current open sports positions with classification
    open_sports = []
    for pos in positions:
        if not _is_sports(pos.market_question):
            continue
        clf = classify_market(pos.market_question)
        mtm_price = last_mtm.get(pos.token_id, pos.avg_cost)
        unrealized = pos.unrealized_pnl(mtm_price)
        open_sports.append({
            "token_id": pos.token_id,
            "market_question": pos.market_question,
            "outcome": pos.outcome,
            "contracts": round(pos.contracts, 2),
            "avg_cost": round(pos.avg_cost, 4),
            "current_price": round(mtm_price, 4),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(pos.realized_pnl, 2),
            "strategy": pos.strategy,
            "bet_type": clf.bet_type,
            "player_name": clf.player_name,
            "team_a": clf.team_a,
            "team_b": clf.team_b,
            "prop_line": clf.prop_line,
            "over_under": clf.over_under,
            "league": clf.league,
            "opened_at": round(pos.opened_at),
        })

    total_closed = len(sports_closed)
    wins = sum(1 for p in sports_closed if p.get("realized_pnl", 0) > 0)
    total_pnl = sum(p.get("realized_pnl", 0.0) for p in sports_closed)

    bet_type_list = [
        {
            "bet_type": k,
            "trades": v["trades"],
            "wins": v["wins"],
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
            "pnl": round(v["pnl"], 2),
        }
        for k, v in sorted(bet_type_stats.items(), key=lambda x: -x[1]["trades"])
    ]

    league_list = [
        {
            "league": k.upper(),
            "trades": v["trades"],
            "wins": v["wins"],
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
            "pnl": round(v["pnl"], 2),
        }
        for k, v in sorted(league_stats.items(), key=lambda x: -x[1]["trades"])
    ]

    # Recent sports trades (last 30)
    recent: list[dict] = []
    for pos in reversed(sports_closed[-30:]):
        q = pos.get("market_question", "")
        clf = classify_market(q)
        recent.append({
            "market_question": q[:80],
            "outcome": pos.get("outcome", ""),
            "pnl": round(pos.get("realized_pnl", 0.0), 2),
            "won": pos.get("realized_pnl", 0) > 0,
            "bet_type": clf.bet_type,
            "player_name": clf.player_name,
            "league": clf.league,
            "strategy": pos.get("strategy", ""),
            "closed_at": pos.get("closed_at", 0),
        })
    recent.reverse()

    return {
        "total_trades": total_closed,
        "wins": wins,
        "win_rate": round(wins / total_closed * 100, 1) if total_closed else 0,
        "total_pnl": round(total_pnl, 2),
        "open_count": len(open_sports),
        "open_positions": open_sports,
        "bet_type_breakdown": bet_type_list,
        "league_breakdown": league_list,
        "recent_trades": recent,
    }


@app.get("/api/optimism_tax/stats")
def optimism_tax_stats():
    """Aggregate stats for the Optimism Tax strategy panel."""
    if not _portfolio:
        return {"trades": [], "summary": {}}
    with _portfolio_lock:
        all_trades = list(_portfolio.trades)

    ot_trades = [t for t in all_trades if t.strategy == "optimism_tax"]

    category_counts: dict = {}
    total_edge = 0.0
    total_mc_p = 0.0
    wins = 0
    losses = 0
    edge_count = 0

    recent: list = []
    for t in reversed(ot_trades):
        meta = getattr(t, "metadata", {}) or {}
        cat = meta.get("category", "unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1
        net_edge = meta.get("net_edge")
        if net_edge is not None:
            total_edge += net_edge
            edge_count += 1
        mc_p = meta.get("mc_p_profit")
        if mc_p is not None:
            total_mc_p += mc_p
        rpnl = getattr(t, "realized_pnl", 0.0) or 0.0
        if t.side == "SELL":
            if rpnl > 0:
                wins += 1
            elif rpnl < 0:
                losses += 1
        if len(recent) < 30:
            recent.append({
                "timestamp": int(t.timestamp),
                "side": t.side,
                "price": round(t.price, 4),
                "usdc": round(t.usdc_amount, 2),
                "realized_pnl": round(rpnl, 4),
                "category": cat,
                "yes_ask": round(meta.get("yes_ask", 0), 4) if meta.get("yes_ask") else None,
                "net_edge": round(net_edge, 4) if net_edge is not None else None,
                "mc_p_profit": round(mc_p, 3) if mc_p is not None else None,
                "true_no_prob": round(meta.get("true_no_prob", 0), 4) if meta.get("true_no_prob") else None,
                "market_question": (meta.get("market_question") or "")[:80],
            })

    n = len(ot_trades)
    resolved = wins + losses
    return {
        "summary": {
            "total_trades": n,
            "avg_edge": round(total_edge / edge_count, 4) if edge_count else 0,
            "avg_mc_p_profit": round(total_mc_p / n, 3) if n else 0,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / resolved * 100, 1) if resolved else None,
        },
        "category_counts": category_counts,
        "recent_trades": recent,
    }


@app.get("/api/optimism_tax/model_viz")
def optimism_tax_model_viz():
    """Return Beta PDF curves and Monte Carlo fan paths for visualization."""
    import math
    import numpy as np

    def _beta_pdf(x: float, a: float, b: float) -> float:
        if x <= 0 or x >= 1:
            return 0.0
        try:
            log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
            log_pdf = (a - 1) * math.log(x) + (b - 1) * math.log(1 - x) - log_beta
            return math.exp(log_pdf)
        except (ValueError, OverflowError):
            return 0.0

    # Pull latest OT trade metadata for real alpha/beta/price
    alpha, beta_param, entry_price, yes_ask = 4.35, 95.65, 0.957, 0.043  # 4.3¢ YES crypto defaults
    size_usdc = 50.0
    if _portfolio:
        with _portfolio_lock:
            ot = [t for t in _portfolio.trades if t.strategy == "optimism_tax"]
        for t in reversed(ot):
            meta = getattr(t, "metadata", {}) or {}
            if meta.get("bayesian_alpha") and meta.get("bayesian_beta"):
                alpha = float(meta["bayesian_alpha"])
                beta_param = float(meta["bayesian_beta"])
                entry_price = float(meta.get("no_entry_price", entry_price))
                yes_ask = float(meta.get("yes_ask", yes_ask))
                size_usdc = float(meta.get("kelly_size", 50.0))
                break

    # --- Beta PDF curves (market-implied vs calibrated) ---
    xs = [round(i / 200, 4) for i in range(1, 200)]  # 0.005 to 0.995
    # Market-implied: Beta centered at yes_ask (same alpha+beta total, different mean)
    n_obs = alpha + beta_param
    mkt_alpha = yes_ask * n_obs
    mkt_beta = (1 - yes_ask) * n_obs
    market_pdf = [round(_beta_pdf(x, mkt_alpha, mkt_beta), 6) for x in xs]
    calibrated_pdf = [round(_beta_pdf(x, alpha, beta_param), 6) for x in xs]

    # --- Monte Carlo fan (80 paths × 50 trades) ---
    n_paths, n_trades = 80, 50
    rng = np.random.default_rng(42)
    win_probs = rng.beta(alpha, beta_param, size=(n_paths, n_trades))
    outcomes = rng.random(size=(n_paths, n_trades))
    wins = outcomes >= win_probs
    contracts = size_usdc / entry_price
    pnl_win = float(contracts * 0.998 - size_usdc)
    pnl_loss = -size_usdc
    per_trade = np.where(wins, pnl_win, pnl_loss)
    cumulative = np.cumsum(per_trade, axis=1)

    # Downsample paths to 40 for the chart (keep every-other)
    paths_out = []
    for i in range(0, n_paths, 2):
        row = [0.0] + [round(float(v), 2) for v in cumulative[i]]
        paths_out.append(row)

    # Median path
    median_path = [0.0] + [round(float(v), 2) for v in np.median(cumulative, axis=0).tolist()]
    p10_path = [0.0] + [round(float(v), 2) for v in np.percentile(cumulative, 10, axis=0).tolist()]
    p90_path = [0.0] + [round(float(v), 2) for v in np.percentile(cumulative, 90, axis=0).tolist()]

    # Kelly formula components
    b = (1 - entry_price) / entry_price * 0.998  # net odds after fee
    true_no_prob = alpha / (alpha + beta_param)
    f_star = max(0.0, (b * true_no_prob - (1 - true_no_prob)) / b)
    kelly_fraction = 0.25
    kelly_f = round(f_star * kelly_fraction, 4)

    return {
        "params": {
            "alpha": round(alpha, 4),
            "beta": round(beta_param, 4),
            "entry_price": round(entry_price, 4),
            "yes_ask": round(yes_ask, 4),
            "size_usdc": round(size_usdc, 2),
            "true_no_prob": round(true_no_prob, 4),
            "pnl_win": round(pnl_win, 2),
            "pnl_loss": round(pnl_loss, 2),
            "b": round(b, 4),
            "f_star": round(f_star, 4),
            "kelly_f": kelly_f,
        },
        "pdf": {
            "xs": xs,
            "market": market_pdf,
            "calibrated": calibrated_pdf,
        },
        "mc": {
            "paths": paths_out,
            "median": median_path,
            "p10": p10_path,
            "p90": p90_path,
            "n_trades": n_trades + 1,
        },
    }


@app.get("/api/sports/live")
async def sports_live():
    """
    Fetch live game data from ESPN for all major leagues.
    Returns live scores, game status, and for open positions with player names,
    fetches live boxscore player stats.
    """
    from src.sports.sports_intel import classify_market, get_sports_intel, _detect_league

    intel = get_sports_intel()

    # Fetch live games for all major leagues
    try:
        all_games = await intel.get_all_live_games()
    except Exception as exc:
        all_games = []

    # Get open sports positions to find relevant players
    with _portfolio_lock:
        open_positions = [
            pos for pos in (_portfolio.positions.values() if _portfolio else [])
            if _SPORTS_KW_MASTER.search(pos.market_question)
        ]

    # For each open sports position, try to find the live game and fetch player stats
    position_game_data: list[dict] = []
    for pos in open_positions:
        clf = classify_market(pos.market_question)
        league = clf.league or _detect_league(pos.market_question) or "nba"

        game = await intel.find_live_game_for_question(pos.market_question, league)
        player_stats: list[dict] = []

        if game and clf.player_name:
            try:
                boxscore = await asyncio.wait_for(
                    intel.get_game_boxscore(game["id"], league),
                    timeout=5.0,
                )
                # Find matching player
                for p in boxscore.get("players", []):
                    p_lower = p["name"].lower()
                    name_lower = clf.player_name.lower()
                    if name_lower in p_lower or p_lower in name_lower:
                        player_stats.append(p)
                        break
            except Exception:
                pass

        position_game_data.append({
            "token_id": pos.token_id,
            "market_question": pos.market_question[:80],
            "bet_type": clf.bet_type,
            "player_name": clf.player_name,
            "league": league,
            "game": game,
            "player_stats": player_stats,
        })

    # Filter to only live and today's games for display
    live_games = [g for g in all_games if g.get("is_live")]
    recent_games = [g for g in all_games if not g.get("is_live")][:20]

    return {
        "live_games": live_games,
        "recent_games": recent_games,
        "position_intel": position_game_data,
        "timestamp": time.time(),
    }


@app.get("/api/weather/stats")
def weather_stats(api_key: str = Depends(_check_api_key)):
    import re as _re
    if not _portfolio:
        return {"total_trades": 0, "wins": 0, "win_rate": 0, "total_pnl": 0, "cities": [], "recent_signals": []}
    with _portfolio_lock:
        trades = list(_portfolio.trades)
    weather_trades = [t for t in trades if getattr(t, 'strategy', '') == 'weather']

    # Parse NOAA notes: "NOAA {city} {date}: model={prob:.1%} market={YES/NO}@{price:.2f} edge={edge:.1%} ..."
    _notes_re = _re.compile(
        r"NOAA (?P<city>[^:]+?) (?P<date>\S+): model=(?P<model>[\d.]+)% "
        r"market=(?P<side>\w+)@(?P<price>[\d.]+) edge=(?P<edge>[\d.]+)%"
    )

    city_stats: dict = {}
    recent_signals = []

    for t in weather_trades:
        notes = getattr(t, 'notes', '') or ''
        m = _notes_re.search(notes)
        city = m.group('city') if m else 'Unknown'
        won = getattr(t, 'pnl', 0) > 0
        pnl = getattr(t, 'pnl', 0)

        if city not in city_stats:
            city_stats[city] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        city_stats[city]['trades'] += 1
        city_stats[city]['wins'] += int(won)
        city_stats[city]['pnl'] += pnl

        if m:
            recent_signals.append({
                'date': m.group('date'),
                'city': city,
                'model_prob': float(m.group('model')),
                'market_price': float(m.group('price')),
                'edge': float(m.group('edge')),
                'side': m.group('side'),
                'pnl': round(pnl, 2),
                'won': won,
            })

    total = len(weather_trades)
    wins = sum(1 for t in weather_trades if getattr(t, 'pnl', 0) > 0)
    total_pnl = sum(getattr(t, 'pnl', 0) for t in weather_trades)

    cities = [
        {
            'city': c,
            'trades': v['trades'],
            'win_rate': round(v['wins'] / v['trades'] * 100, 1) if v['trades'] else 0,
            'pnl': round(v['pnl'], 2),
        }
        for c, v in city_stats.items()
    ]
    cities.sort(key=lambda x: x['pnl'], reverse=True)

    # Most recent 20 signals
    recent_signals = recent_signals[-20:]
    recent_signals.reverse()

    return {
        'total_trades': total,
        'wins': wins,
        'win_rate': round(wins / total * 100, 1) if total else 0,
        'total_pnl': round(total_pnl, 2),
        'cities': cities,
        'recent_signals': recent_signals,
    }


# ------------------------------------------------------------------ #
#  Sports live-scores endpoint                                         #
# ------------------------------------------------------------------ #

@app.get("/api/sports-scores")
async def sports_scores():
    """
    Live game scores for open sports positions.
    Uses ESPN's free unofficial scoreboard API — no API key, no cost.
    Cached 30 s server-side so rapid dashboard refreshes are free.
    """
    if not _portfolio:
        return []

    with _portfolio_lock:
        positions_items = list(_portfolio.positions.items())
    with _market_status_lock:
        status_snapshot = dict(_market_status)

    # Determine which ESPN sport feeds we need
    sports_needed: set[str] = set()
    pos_sport: dict[str, str] = {}   # token_id -> sport_key
    unresolved: list[str] = []       # token_ids with unknown sport — try all feeds
    for tid, pos in positions_items:
        ms = status_snapshot.get(tid, {})
        cat = ms.get("category", "")
        sport_key = _detect_sport(pos.market_question, cat)
        if sport_key:
            sports_needed.add(sport_key)
            pos_sport[tid] = sport_key
        else:
            unresolved.append(tid)

    # For unresolved positions, try all main sport feeds and match by team name
    if unresolved:
        for sk in _ALL_SPORT_KEYS:
            sports_needed.add(sk)

    if not sports_needed:
        return []

    # Parallel-fetch ESPN feeds for all needed sports
    import asyncio as _aio
    sport_list = list(sports_needed)
    fetched = await _aio.gather(*[_fetch_espn(s) for s in sport_list], return_exceptions=True)
    sport_events: dict[str, list] = {}
    for sk, result in zip(sport_list, fetched):
        if isinstance(result, list):
            sport_events[sk] = result

    # Match each position to a game
    results = []
    for tid, pos in positions_items:
        sport_key = pos_sport.get(tid)
        teams = _extract_teams(pos.market_question)
        matched = None
        matched_sport = sport_key or ""

        if teams:
            t1, t2 = teams
            # Search specific sport first, then all loaded sports
            search_order = ([sport_key] if sport_key else []) + [
                sk for sk in sport_events if sk != sport_key
            ]
            for sk in search_order:
                for ev in sport_events.get(sk, []):
                    ev_teams = ev.get("teams", [])
                    names = [t["name"] for t in ev_teams]
                    abbrs = [t["abbr"] for t in ev_teams]
                    if any(_team_matches(t1, n, a) for n, a in zip(names, abbrs)) or \
                       any(_team_matches(t2, n, a) for n, a in zip(names, abbrs)):
                        matched = ev
                        matched_sport = sk
                        break
                if matched:
                    break

        # Only include if we detected a sport (known category OR matched a game)
        if not sport_key and not matched:
            continue
        results.append({
            "token_id": tid,
            "question": pos.market_question[:80],
            "outcome": pos.outcome,
            "strategy": pos.strategy,
            "cost_basis": round(pos.cost_basis, 2),
            "sport": matched_sport.upper(),
            "game": matched,
        })

    return results


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _fmt_uptime(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s"


# ------------------------------------------------------------------ #
#  Dashboard HTML                                                      #
# ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Arb Bot</title>
<script>
fetch('/api/bot_info').then(r=>r.json()).then(d=>{
  document.title=d.name+' — Polymarket Arb Bot';
  const el=document.getElementById('bot-title');
  if(!el)return;
  const isB=(d.name||'').toLowerCase().includes('b');
  const badge=isB
    ? '<span style="font-size:0.6em;background:#6366f1;color:#fff;padding:2px 10px;border-radius:9999px;vertical-align:middle;margin-left:8px">'+d.name+'</span>'
    : '<span style="font-size:0.6em;background:#10b981;color:#000;padding:2px 10px;border-radius:9999px;vertical-align:middle;margin-left:8px">'+d.name+'</span>';
  el.innerHTML='Polymarket Arb Bot'+badge;
});
</script>

<style>
:root{
  --bg:#0a0a0f;
  --surface:#12121a;
  --surface2:#1a1a26;
  --border:#1e1e2e;
  --accent:#7c3aed;
  --accent2:#06b6d4;
  --green:#10b981;
  --red:#ef4444;
  --yellow:#f59e0b;
  --text:#e2e8f0;
  --muted:#64748b;
  --card-radius:12px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:16px}
header h1{color:var(--accent2);font-size:1.1rem;letter-spacing:.05em;font-weight:700}
#mode-badge{font-size:.7rem;padding:3px 10px;border-radius:6px;background:#0e1a2a;color:var(--accent2);border:1px solid #1a3a5a;font-weight:600}
#uptime-info{color:var(--muted);font-size:.75rem;margin-left:auto}
#reset-btn{font-size:.7rem;padding:4px 12px;border-radius:6px;background:#1f0a0a;color:var(--red);border:1px solid #3d1515;cursor:pointer;transition:all .2s}
#reset-btn:hover{background:#3d1515}
#add-funds-btn{font-size:.7rem;padding:4px 12px;border-radius:6px;background:#0a1f0a;color:var(--green);border:1px solid #153d15;cursor:pointer;transition:all .2s}
#add-funds-btn:hover{background:#153d15}

.tabs{display:flex;background:var(--surface);border-bottom:1px solid var(--border);padding:0 20px;gap:2px}
.tab{padding:11px 20px;cursor:pointer;color:var(--muted);font-size:.8rem;font-weight:500;border-bottom:2px solid transparent;transition:all .2s;white-space:nowrap}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent2);border-bottom-color:var(--accent2)}

.page{display:none;padding:20px;animation:fadein .2s;max-width:1600px}
.page.active{display:block}
@keyframes fadein{from{opacity:0}to{opacity:1}}

/* Metric cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);padding:16px;position:relative;overflow:hidden}
.card::after{content:'';position:absolute;bottom:0;left:0;right:0;height:3px;background:var(--card-accent,var(--border));border-radius:0 0 var(--card-radius) var(--card-radius)}
.card .lbl{color:var(--muted);font-size:.63rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:7px;font-weight:600}
.card .val{font-size:1.45rem;font-weight:700;color:var(--text);line-height:1}
.card .sub{color:var(--muted);font-size:.65rem;margin-top:5px}
.card .val.green{color:var(--green)}.card .val.red{color:var(--red)}.card .val.blue{color:var(--accent2)}.card .val.yellow{color:var(--yellow)}.card .val.purple{color:#a78bfa}

.card-pnl{--card-accent:var(--green)}
.card-balance{--card-accent:var(--accent2)}
.card-winrate{--card-accent:var(--yellow)}
.card-trades{--card-accent:var(--accent)}

/* Chart boxes */
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}
.chart-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);padding:16px}
.chart-box h3{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px;font-weight:600}
.chart-box canvas{max-height:200px}

/* Sections */
.section{background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);padding:16px;margin-bottom:14px}
.section h3{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px;font-weight:600}

table{width:100%;border-collapse:collapse}
th{text-align:left;color:var(--muted);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--border);font-size:.67rem;text-transform:uppercase;letter-spacing:.05em}
td{padding:6px 8px;border-bottom:1px solid var(--surface2);font-size:.75rem;color:var(--text)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--surface2)}
.buy{color:var(--green)}.sell{color:var(--red)}.win{color:var(--green)}.loss{color:var(--red)}

.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.63rem;font-weight:700;letter-spacing:.04em}
.badge.combinatorial{background:#0d0d2a;color:#818cf8}
.badge.latency_arb{background:#1f0d0d;color:#fb923c}
.badge.market_making{background:#1f1a00;color:var(--yellow)}
.badge.resolution{background:#0d1f1f;color:var(--accent2)}
.badge.event_driven{background:#1a0d2a;color:#c084fc}
.badge.cross_exchange{background:#0d1a2a;color:#60a5fa}
.badge.futures_hedge{background:#1a0d1a;color:#e879f9}
.badge.swarm{background:#0d1a1a;color:#34d399}
.badge.quick_resolution{background:#1a1a0d;color:#fbbf24}
.badge.crypto_5m{background:#0d0d1a;color:#a5b4fc}

/* Strategy bars */
.strat-bars{display:flex;flex-direction:column;gap:8px}
.strat-row{display:flex;align-items:center;gap:10px}
.strat-row .name{width:160px;font-size:.73rem;color:var(--muted)}
.strat-row .bar-wrap{flex:1;background:var(--bg);border-radius:4px;height:22px;overflow:hidden}
.strat-row .bar{height:100%;border-radius:4px;display:flex;align-items:center;padding:0 10px;font-size:.7rem;font-weight:700;min-width:60px;transition:width .5s}
.bar.pos{background:#0a2a1a;color:var(--green)}.bar.neg{background:#2a0a0a;color:var(--red)}

/* Log feed */
#log-feed{background:var(--bg);border:1px solid var(--border);border-radius:var(--card-radius);height:500px;overflow-y:auto;padding:10px;font-family:'Cascadia Code','Consolas',monospace;font-size:.71rem}
.log-line{padding:2px 0;border-bottom:1px solid var(--surface);line-height:1.5}
.log-line .ts{color:#3a3a5a;margin-right:8px}
.log-line .lvl{margin-right:8px;font-weight:700}
.log-line .lvl.INFO{color:var(--accent2)}.log-line .lvl.WARNING{color:var(--yellow)}.log-line .lvl.ERROR{color:var(--red)}.log-line .lvl.DEBUG{color:#3a3a5a}.log-line .lvl.SUCCESS{color:var(--green)}

/* Meta cards */
.meta-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);padding:16px;margin-bottom:14px}
.meta-card h3{color:#818cf8;margin-bottom:10px;font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.meta-analysis{color:#cbd5e1;font-size:.76rem;line-height:1.7;white-space:pre-wrap;max-height:320px;overflow-y:auto}
.change-table td:nth-child(3){color:var(--green)}
.ts-small{color:var(--muted);font-size:.65rem}
.no-data{color:#2a2a3a;text-align:center;padding:32px;font-size:.8rem}
#last-update{color:#2a2a3a;font-size:.65rem;text-align:right;padding:6px 20px}

/* Status / System tab */
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-bottom:18px}
.status-item{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px;display:flex;align-items:center;gap:10px}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.dot.ok{background:var(--green);box-shadow:0 0 8px var(--green)}
.dot.warn{background:var(--yellow);box-shadow:0 0 8px var(--yellow)}
.dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
.status-label{font-size:.76rem;color:var(--text);font-weight:500}
.status-detail{font-size:.63rem;color:var(--muted);margin-top:2px}

/* Strategy cards for Strategies tab */
.strat-card{background:var(--surface2);border:1px solid var(--border);border-radius:var(--card-radius);padding:14px}
.strat-card.enabled{border-color:#1a3a2a}
.strat-card.disabled{border-color:#2a1a1a;opacity:.55}
.strat-card h4{font-size:.8rem;font-weight:700;margin-bottom:6px;color:var(--text)}
.strat-card .strat-status{font-size:.63rem;font-weight:700}
.strat-card .strat-note{font-size:.63rem;color:var(--muted);margin-top:5px;line-height:1.5}
.strat-card .strat-metrics{display:flex;gap:12px;margin-top:8px;flex-wrap:wrap}
.strat-metric{display:flex;flex-direction:column;gap:2px}
.strat-metric .sm-lbl{font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.strat-metric .sm-val{font-size:.82rem;font-weight:700}

.health-bar{height:6px;border-radius:3px;background:var(--surface2);overflow:hidden;margin-top:6px}
.health-fill{height:100%;border-radius:3px;transition:width .5s}

/* Analytics / Strategies tab */
.analytics-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.thinking-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);padding:16px;margin-bottom:14px}
.thinking-card h3{font-size:.68rem;color:#818cf8;text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px;font-weight:600}

/* Time-to-real-PnL widget */
.ttpl-card{background:var(--surface);border:1px solid var(--border);border-top:3px solid var(--green);border-radius:var(--card-radius);padding:16px;margin-bottom:20px}
.ttpl-header{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.ttpl-title{font-size:.82rem;font-weight:700;color:var(--text);text-transform:uppercase;letter-spacing:.06em}
.ttpl-badge{font-size:.68rem;font-weight:700;padding:3px 10px;border-radius:6px;background:#0d2a0d;color:var(--green)}
.ttpl-badge.bootstrap{background:#2a1a00;color:var(--yellow)}
.ttpl-badge.active{background:#0d1a0d;color:var(--green)}
.ttpl-badge.profit{background:#0d1a2a;color:var(--accent2)}
.ttpl-milestones{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:12px}
.ttpl-milestone{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px}
.ttpl-milestone.done{border-color:#1a3a2a}
.ttpl-milestone.done .ttpl-ms-eta{color:var(--green)}
.ttpl-ms-label{font-size:.63rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}
.ttpl-ms-eta{font-size:1.1rem;font-weight:700;color:var(--yellow);margin-bottom:4px}
.ttpl-ms-sub{font-size:.63rem;color:var(--muted)}
.ttpl-ms-bar{height:4px;background:var(--bg);border-radius:2px;margin-top:8px;overflow:hidden}
.ttpl-ms-fill{height:100%;border-radius:2px;transition:width .5s}
.ttpl-verdict{font-size:.76rem;color:var(--muted);line-height:1.7;padding-top:10px;border-top:1px solid var(--border)}

/* Balances absorbed into System tab */
.bal-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-bottom:18px}
.bal-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);padding:18px}
.bal-card.anthropic{border-top:3px solid #a78bfa}
.bal-card.railway{border-top:3px solid var(--accent2)}
.bal-card.bot{border-top:3px solid var(--green)}
.bal-card h3{font-size:.78rem;font-weight:700;margin-bottom:14px;text-transform:uppercase;letter-spacing:.06em}
.bal-card.anthropic h3{color:#a78bfa}
.bal-card.railway h3{color:var(--accent2)}
.bal-card.bot h3{color:var(--green)}
.bal-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border);font-size:.76rem}
.bal-row:last-child{border-bottom:none}
.bal-lbl{color:var(--muted)}
.bal-val{font-weight:600;color:var(--text)}
.budget-bar{height:5px;border-radius:3px;background:var(--surface2);overflow:hidden;margin-top:10px}
.budget-fill{height:100%;border-radius:3px;transition:width .5s}
.budget-label{font-size:.63rem;color:var(--muted);margin-top:4px;display:flex;justify-content:space-between}
.cycle-bar{background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);padding:16px;margin-bottom:18px}
.cycle-bar h3{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px;font-weight:600}
.cycle-progress{height:8px;border-radius:4px;background:var(--surface2);overflow:hidden;margin-bottom:8px}
.cycle-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accent2),var(--accent));transition:width .5s}
.refill-link{display:block;margin-top:12px;text-align:center;font-size:.7rem;font-weight:600;padding:7px;border-radius:8px;background:var(--surface2);border:1px solid var(--border);color:var(--muted);text-decoration:none;transition:all .2s}
.refill-link:hover{background:var(--border);color:var(--text)}

/* Research / AI Intel */
.res-insights-card{background:var(--surface);border:1px solid var(--border);border-top:3px solid var(--yellow);border-radius:var(--card-radius);padding:16px;margin-bottom:14px}
.res-insights-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.res-run-label{font-size:.63rem;color:var(--muted)}
.res-insight{display:flex;align-items:flex-start;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);font-size:.76rem;color:#cbd5e1;line-height:1.6}
.res-insight:last-child{border-bottom:none}
.res-insight-bullet{color:var(--yellow);font-size:.85rem;flex-shrink:0;margin-top:1px}
.res-finding{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px;border-left:3px solid var(--border)}
.res-finding.high{border-left-color:var(--green)}
.res-finding.medium{border-left-color:var(--yellow)}
.res-finding.low{border-left-color:var(--muted)}
.res-finding-meta{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.res-rel{font-size:.63rem;font-weight:700;padding:2px 7px;border-radius:3px;text-transform:uppercase}
.res-rel.high{background:#0a2a1a;color:var(--green)}
.res-rel.medium{background:#2a1a00;color:var(--yellow)}
.res-rel.low{background:var(--surface2);color:var(--muted)}
.res-cat{font-size:.63rem;background:#0d0d2a;color:#818cf8;padding:2px 7px;border-radius:3px}
.res-source{font-size:.63rem;color:var(--muted);font-style:italic}
.res-title{font-size:.8rem;font-weight:600;color:var(--text);margin-bottom:4px}
.res-summary{font-size:.73rem;color:var(--muted);line-height:1.5;margin-bottom:5px}
.res-suggestion{font-size:.7rem;color:var(--accent2);padding:5px 8px;background:#0a1a2a;border-radius:4px}
.res-experiment{display:flex;align-items:flex-start;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);font-size:.76rem;color:#cbd5e1}
.res-experiment:last-child{border-bottom:none}
.res-exp-num{color:#818cf8;font-weight:700;flex-shrink:0;min-width:18px}

/* Code Review (inside System tab) */
.cr-finding{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px;border-left:3px solid var(--border)}
.cr-finding.high{border-left-color:var(--red)}
.cr-finding.medium{border-left-color:var(--yellow)}
.cr-finding.low{border-left-color:var(--accent2)}
.cr-finding.info{border-left-color:var(--muted)}
.cr-finding-header{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.cr-sev{font-size:.63rem;font-weight:700;padding:2px 7px;border-radius:3px;text-transform:uppercase}
.cr-sev.high{background:#2a0a0a;color:var(--red)}
.cr-sev.medium{background:#2a1a00;color:var(--yellow)}
.cr-sev.low{background:#0a1a2a;color:var(--accent2)}
.cr-sev.info{background:var(--surface2);color:var(--muted)}
.cr-cat{font-size:.63rem;color:var(--muted);font-style:italic}
.cr-file{font-size:.63rem;color:#818cf8;font-family:monospace}
.cr-title{font-size:.8rem;font-weight:600;color:var(--text)}
.cr-desc{font-size:.73rem;color:var(--muted);margin-top:4px;line-height:1.5}
.cr-suggestion{font-size:.7rem;color:var(--accent2);margin-top:5px;padding:5px 8px;background:#0a1a2a;border-radius:4px}
.cr-strength{font-size:.76rem;color:var(--green);padding:4px 0;display:flex;align-items:flex-start;gap:6px}
.cr-grade-A{color:var(--green)}.cr-grade-B{color:var(--accent2)}.cr-grade-C{color:var(--yellow)}.cr-grade-D{color:#fb923c}.cr-grade-F{color:var(--red)}

/* AI Intel status cards */
.ai-status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-bottom:20px}
.ai-status-card{background:var(--surface2);border:1px solid var(--border);border-radius:var(--card-radius);padding:18px}
.ai-status-card.configured{border-color:#1a3a2a;border-top:3px solid var(--green)}
.ai-status-card.missing{border-top:3px solid var(--muted)}
.ai-card-name{font-size:.9rem;font-weight:700;color:var(--text);margin-bottom:4px}
.ai-card-status{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.ai-card-note{font-size:.68rem;color:var(--muted);line-height:1.5}
.ai-card-key{font-size:.63rem;color:var(--muted);font-family:monospace;margin-top:6px;padding:4px 8px;background:var(--bg);border-radius:4px}
</style>
</head>
<body>

<header>
  <h1 id="bot-title">Polymarket Arb Bot</h1>
  <span id="mode-badge">PAPER</span>
  <span id="uptime-info">loading...</span>
  <button id="add-funds-btn" onclick="addFunds()">+ Add $10k</button>
  <button id="reset-btn" onclick="resetPortfolio()">Reset to $10,000</button>
</header>

<div class="tabs">
  <div class="tab active" onclick="showTab('overview')">Overview</div>
  <div class="tab" onclick="showTab('positions')">Positions</div>
  <div class="tab" onclick="showTab('trades')">Trades</div>
  <div class="tab" onclick="showTab('strategies')">Strategies</div>
  <div class="tab" onclick="showTab('ai-intel')">AI Intel</div>
  <div class="tab" onclick="showTab('meta')">Meta-Agent</div>
  <div class="tab" onclick="showTab('system')">System</div>
  <div class="tab" onclick="showTab('weather')">Weather</div>
  <div class="tab" onclick="showTab('sports')">Sports</div>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB 1: OVERVIEW                                            -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page active" id="tab-overview">

  <div style="display:flex;justify-content:flex-end;margin-bottom:14px;gap:8px">
    <a id="poly-profile-link" href="https://polymarket.com/portfolio" target="_blank"
       style="display:inline-flex;align-items:center;gap:6px;background:var(--surface2);color:var(--accent2);
              padding:7px 14px;border-radius:8px;font-size:.72rem;font-weight:700;
              text-decoration:none;border:1px solid var(--border);transition:background .15s"
       onmouseover="this.style.background='var(--border)'" onmouseout="this.style.background='var(--surface2)'">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
      Polymarket Profile
    </a>
    <a href="https://polymarket.com/portfolio" target="_blank" id="poly-portfolio-link"
       style="display:inline-flex;align-items:center;gap:6px;background:var(--surface2);color:var(--green);
              padding:7px 14px;border-radius:8px;font-size:.72rem;font-weight:700;
              text-decoration:none;border:1px solid var(--border);transition:background .15s"
       onmouseover="this.style.background='var(--border)'" onmouseout="this.style.background='var(--surface2)'">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
      Portfolio
    </a>
  </div>

  <!-- Top 4 metric cards -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px">
    <div class="card card-pnl">
      <div class="lbl">Total P&amp;L</div>
      <div class="val" id="total-value" style="font-size:1.6rem">--</div>
      <div class="sub" id="total-pnl-sub">--</div>
    </div>
    <div class="card card-balance">
      <div class="lbl">Cash Balance</div>
      <div class="val blue" id="balance" style="font-size:1.6rem">--</div>
    </div>
    <div class="card card-winrate">
      <div class="lbl">Win Rate</div>
      <div class="val" id="win-rate" style="font-size:1.6rem">--</div>
      <div class="sub">closed positions</div>
    </div>
    <div class="card card-trades">
      <div class="lbl">Trades Today</div>
      <div class="val purple" id="trades-per-hr" style="font-size:1.6rem">--</div>
      <div class="sub" id="total-trades-sub">-- total</div>
    </div>
  </div>

  <!-- Second row: realized P&L, positions, exposure, fees -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px">
    <div class="card">
      <div class="lbl">Realized P&amp;L</div>
      <div class="val" id="realized-pnl">--</div>
      <div class="sub" id="realized-pnl-pct">-- | <span id="closed-count">0</span> closed</div>
    </div>
    <div class="card">
      <div class="lbl">Open Positions</div>
      <div class="val yellow" id="pos-count">--</div>
      <div class="sub" id="exposure-sub">--</div>
    </div>
    <div class="card">
      <div class="lbl">Fees Paid</div>
      <div class="val red" id="fees">--</div>
    </div>
    <div class="card">
      <div class="lbl">Cycle Count</div>
      <div class="val blue" id="live-cycle">--</div>
      <div class="sub" id="live-uptime-sub">--</div>
    </div>
  </div>

  <!-- Charts -->
  <div class="chart-grid">
    <div class="chart-box">
      <h3>Portfolio Value Over Time</h3>
      <canvas id="pnlChart"></canvas>
    </div>
    <div class="chart-box">
      <h3>Trades Per Strategy</h3>
      <canvas id="stratChart"></canvas>
    </div>
  </div>

  <!-- Strategy P&L bars -->
  <div class="section">
    <h3>Strategy P&amp;L</h3>
    <div class="strat-bars" id="strat-bars"><div class="no-data">Waiting for trades...</div></div>
  </div>

  <!-- Agent countdown timers -->
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px">
    <div style="background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:var(--card-radius);padding:14px">
      <div style="font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);font-weight:700;margin-bottom:5px">Meta-Agent</div>
      <div style="font-size:1.2rem;font-weight:700;font-family:monospace;color:var(--text)" id="timer-meta">--</div>
      <div style="font-size:.62rem;color:var(--muted);margin-top:3px" id="timer-meta-sub">last run --</div>
    </div>
    <div style="background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent2);border-radius:var(--card-radius);padding:14px">
      <div style="font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--accent2);font-weight:700;margin-bottom:5px">Research Agent</div>
      <div style="font-size:1.2rem;font-weight:700;font-family:monospace;color:var(--text)" id="timer-research">--</div>
      <div style="font-size:.62rem;color:var(--muted);margin-top:3px" id="timer-research-sub">last run --</div>
    </div>
    <div style="background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--yellow);border-radius:var(--card-radius);padding:14px">
      <div style="font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--yellow);font-weight:700;margin-bottom:5px">Code Review</div>
      <div style="font-size:1.2rem;font-weight:700;font-family:monospace;color:var(--text)" id="timer-review">--</div>
      <div style="font-size:.62rem;color:var(--muted);margin-top:3px" id="timer-review-sub">last run --</div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB 2: POSITIONS                                           -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page" id="tab-positions">
  <div class="section">
    <h3>Open Positions (<span id="open-pos-count">0</span>)</h3>
    <div id="positions-table"><div class="no-data">No open positions</div></div>
  </div>
  <div class="section">
    <h3>Closed Positions — Recent 100 <span style="color:var(--muted);font-weight:normal;font-size:.63rem">Realized results only</span></h3>
    <div id="closed-table"><div class="no-data">No closed positions yet</div></div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB 3: TRADES                                              -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page" id="tab-trades">
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px">
    <div class="card"><div class="lbl">Total Trades</div><div class="val blue" id="t-total">--</div></div>
    <div class="card"><div class="lbl">Buys</div><div class="val green" id="t-buys">--</div></div>
    <div class="card"><div class="lbl">Sells</div><div class="val red" id="t-sells">--</div></div>
    <div class="card"><div class="lbl">Fees Paid</div><div class="val yellow" id="t-fees">--</div></div>
  </div>
  <div class="section">
    <h3>Recent Trades (last 100)</h3>
    <div id="trades-table"><div class="no-data">No trades yet</div></div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB 4: STRATEGIES  (was Analytics + Status strategy cards) -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page" id="tab-strategies">

  <!-- Risk Health Score (from Status) -->
  <div class="section" style="margin-bottom:14px">
    <h3>Risk Health</h3>
    <div id="status-risk"><div class="no-data">Loading...</div></div>
  </div>

  <!-- Strategy Cards -->
  <div class="section" style="margin-bottom:14px">
    <h3>Strategies</h3>
    <div class="status-grid" id="status-strategies">
      <div class="no-data">Loading...</div>
    </div>
  </div>

  <!-- Optimism Tax Deep-Dive Panel -->
  <div style="margin-bottom:14px" id="ot-panel">
    <!-- Header bar -->
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
      <h3 style="margin:0;font-size:.85rem;font-weight:700;letter-spacing:.04em;color:var(--text)">OPTIMISM TAX</h3>
      <span style="font-size:.6rem;font-weight:700;background:#0a1a2a;color:#06b6d4;padding:2px 10px;border-radius:10px;letter-spacing:.08em">ARB ENGINE // BTC LIMIT ORDER BOT</span>
      <span style="font-size:.6rem;color:var(--muted);margin-left:auto" id="ot-block-info">EDGE: --</span>
    </div>

    <!-- Summary metrics strip -->
    <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:12px">
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:8px 10px">
        <div style="font-size:.55rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Trades</div>
        <div style="font-size:1.2rem;font-weight:700;color:var(--text)" id="ot-total">--</div>
      </div>
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:8px 10px">
        <div style="font-size:.55rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Avg Edge</div>
        <div style="font-size:1.2rem;font-weight:700;color:#06b6d4" id="ot-edge">--</div>
      </div>
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:8px 10px">
        <div style="font-size:.55rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">MC P(profit)</div>
        <div style="font-size:1.2rem;font-weight:700;color:var(--green)" id="ot-mcp">--</div>
      </div>
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:8px 10px">
        <div style="font-size:.55rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Win Rate</div>
        <div style="font-size:1.2rem;font-weight:700;color:var(--green)" id="ot-wr">--</div>
      </div>
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:8px 10px">
        <div style="font-size:.55rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">W / L</div>
        <div style="font-size:1.2rem;font-weight:700;color:var(--text)" id="ot-wl">-- / --</div>
      </div>
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:8px 10px">
        <div style="font-size:.55rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Strategy</div>
        <div style="font-size:.75rem;font-weight:700;color:var(--yellow);margin-top:4px">LIMIT</div>
      </div>
    </div>

    <!-- Three model charts row -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px">

      <!-- Bayesian Model -->
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px">
        <div style="font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:2px">Bayesian Model</div>
        <div style="font-size:.65rem;color:#555;margin-bottom:8px" id="ot-bay-subtitle">β(α,β) posterior</div>
        <canvas id="ot-bayesian-chart" style="max-height:160px"></canvas>
        <div style="display:flex;gap:14px;margin-top:8px;font-size:.6rem;color:var(--muted)">
          <span style="display:flex;align-items:center;gap:4px"><span style="display:inline-block;width:10px;height:2px;background:#ef4444;border-radius:1px"></span>Market</span>
          <span style="display:flex;align-items:center;gap:4px"><span style="display:inline-block;width:10px;height:2px;background:#06b6d4;border-radius:1px"></span>Calibrated</span>
        </div>
      </div>

      <!-- Monte Carlo Fan -->
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px">
        <div style="font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:2px">Monte Carlo</div>
        <div style="font-size:.65rem;color:#555;margin-bottom:8px" id="ot-mc-subtitle">N=1000 simulation paths</div>
        <canvas id="ot-mc-chart" style="max-height:160px"></canvas>
        <div style="display:flex;gap:14px;margin-top:8px;font-size:.6rem;color:var(--muted)">
          <span style="display:flex;align-items:center;gap:4px"><span style="display:inline-block;width:10px;height:2px;background:#10b981;border-radius:1px"></span>Median</span>
          <span style="display:flex;align-items:center;gap:4px"><span style="display:inline-block;width:10px;height:2px;background:#1e293b;border-radius:1px;border:1px solid #334155"></span>Paths</span>
        </div>
      </div>

      <!-- Kelly Sizing -->
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px">
        <div style="font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:2px">Kelly Criterion</div>
        <div style="font-size:.65rem;color:#555;margin-bottom:10px">f* = (b·p − q) / b × 0.25</div>
        <div id="ot-kelly-display" style="font-size:.68rem;line-height:2;font-family:monospace;color:var(--text)">
          <div class="no-data">Loading...</div>
        </div>
      </div>
    </div>

    <!-- Bottom row: P&L curve + category + trades -->
    <div style="display:grid;grid-template-columns:1fr 180px 1fr;gap:10px">

      <!-- P&L Curve -->
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px">
        <div style="font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">P&amp;L Curve</div>
        <div style="font-size:1.6rem;font-weight:700;color:var(--green);margin-bottom:8px" id="ot-pnl-total">$0.00</div>
        <canvas id="ot-pnl-chart" style="max-height:120px"></canvas>
      </div>

      <!-- Category breakdown -->
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px">
        <div style="font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px">Categories</div>
        <div id="ot-categories" style="font-size:.68rem"></div>
      </div>

      <!-- Bot status panel -->
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px">
        <div style="font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px">Bot Status</div>
        <div id="ot-bot-status" style="font-size:.7rem;line-height:2;font-family:monospace">
          <div class="no-data">Loading...</div>
        </div>
      </div>
    </div>

    <!-- Recent trades table -->
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px;margin-top:10px">
      <div style="font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px">Recent NO Trades</div>
      <div id="ot-recent-trades" style="font-size:.72rem;max-height:200px;overflow-y:auto">
        <div class="no-data">No trades yet</div>
      </div>
    </div>
  </div>

  <!-- Time-to-real-PnL estimator -->
  <div class="ttpl-card" id="ttpl-card">
    <div class="ttpl-header">
      <span class="ttpl-title">Time to Real P&amp;L</span>
      <span class="ttpl-badge" id="ttpl-phase-badge">--</span>
    </div>
    <div>
      <div class="ttpl-milestones" id="ttpl-milestones">
        <div class="no-data">Loading estimates...</div>
      </div>
      <div class="ttpl-verdict" id="ttpl-verdict"></div>
    </div>
  </div>

  <!-- Analytics charts -->
  <div class="analytics-row">
    <div class="chart-box">
      <h3>Strategy ROI %</h3>
      <canvas id="roiChart" style="max-height:220px"></canvas>
    </div>
    <div class="chart-box">
      <h3>Strategy Win Rate %</h3>
      <canvas id="winRateChart" style="max-height:220px"></canvas>
    </div>
  </div>

  <div class="analytics-row">
    <div class="chart-box">
      <h3>Hourly PnL — Last 24h</h3>
      <canvas id="hourlyPnlChart" style="max-height:220px"></canvas>
    </div>
    <div class="chart-box">
      <h3>Fee Drag Per Strategy</h3>
      <canvas id="feeDragChart" style="max-height:220px"></canvas>
    </div>
  </div>

  <div class="section">
    <h3>Health Score Trend (Meta-Agent History)</h3>
    <canvas id="healthTrendChart" style="max-height:160px"></canvas>
  </div>

  <div class="thinking-card">
    <h3>Recent LLM Decisions</h3>
    <div id="llm-decisions-table"><div class="no-data">No LLM-tagged trades yet</div></div>
  </div>

  <div class="thinking-card">
    <h3>Active LLM Signals (Last Hour)</h3>
    <div id="llm-active-signals"><div class="no-data">None in last hour</div></div>
  </div>

  <div class="thinking-card">
    <h3>Meta-Agent Parameter Change Timeline</h3>
    <div id="param-timeline"><div class="no-data">No parameter changes yet</div></div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB 5: AI INTEL  (was AI Intel + Research)                 -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page" id="tab-ai-intel">

  <!-- AI Service Status Cards -->
  <div class="ai-status-grid" id="ai-intel-status-cards">
    <div class="no-data">Loading AI integrations...</div>
  </div>

  <!-- Ensemble LLM Signals -->
  <div class="section" style="margin-bottom:14px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <h3>Ensemble LLM Signals</h3>
      <span id="ensemble-status" style="font-size:.65rem;color:var(--muted)">--</span>
    </div>
    <table>
      <thead><tr>
        <th>Market</th><th>Claude</th><th>OpenAI</th><th>Consensus</th><th>Age (min)</th>
      </tr></thead>
      <tbody id="ensemble-tbody">
        <tr><td colspan="5" style="color:var(--muted);text-align:center">No evaluations yet</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Active Research Signals -->
  <div class="meta-card" id="res-signals-card" style="margin-bottom:14px">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px">
      <h3 style="margin:0">Active Research Signals</h3>
      <span id="res-signals-status" style="font-size:.66rem;color:var(--muted)">injected into combinatorial strategy</span>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <span style="font-size:.66rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Hot topics:</span>
      <div id="res-active-topics" style="display:flex;gap:6px;flex-wrap:wrap"><span style="color:var(--muted);font-size:.66rem">none yet</span></div>
    </div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <span style="font-size:.66rem"><span style="color:var(--muted)">Focus:</span> <span id="res-signal-focus" style="color:var(--yellow)">--</span></span>
      <span style="font-size:.66rem"><span style="color:var(--muted)">Confidence:</span> <span id="res-signal-confidence">--</span></span>
      <span style="font-size:.66rem"><span style="color:var(--muted)">Param hints:</span> <span id="res-signal-params" style="font-family:monospace;color:var(--accent2)">none</span></span>
    </div>
  </div>

  <!-- Top insights from latest research run -->
  <div class="res-insights-card" id="res-insights-card">
    <div class="res-insights-header">
      <span style="font-size:.78rem;font-weight:700;color:var(--yellow);text-transform:uppercase;letter-spacing:.06em">Research Top Insights</span>
      <span class="res-run-label" id="res-run-label">--</span>
    </div>
    <div id="res-insights"><div class="no-data">No research yet — runs every 2 hours (RESEARCH_INTERVAL_HOURS)</div></div>
  </div>

  <!-- Research run header stats -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
    <div class="card"><div class="lbl">Last Research Run</div><div class="val blue" id="res-last">--</div></div>
    <div class="card"><div class="lbl">Next Research Run</div><div class="val yellow" id="res-next">--</div></div>
    <div class="card"><div class="lbl">Total Findings</div><div class="val" id="res-total">--</div><div class="sub" id="res-high">--</div></div>
    <div class="card"><div class="lbl">Web Search</div><div class="val" id="res-websearch">--</div><div class="sub" id="res-interval">every -- h</div></div>
  </div>

  <!-- Research findings -->
  <div class="section" style="margin-top:14px">
    <h3>Findings <span style="color:var(--muted);font-weight:normal;font-size:.63rem" id="res-topics-label"></span></h3>
    <div id="res-findings"><div class="no-data">No findings yet.</div></div>
  </div>

  <!-- Suggested experiments -->
  <div class="section" style="margin-top:14px">
    <h3>Suggested Experiments</h3>
    <div id="res-experiments"><div class="no-data">No suggestions yet.</div></div>
  </div>

  <!-- Research run history -->
  <div class="section" style="margin-top:14px">
    <h3>Research History <span style="color:var(--muted);font-weight:normal;font-size:.63rem">(last 24 runs)</span></h3>
    <div id="res-history"><div class="no-data">No history yet.</div></div>
  </div>

  <!-- Strategy Proposals -->
  <div class="section" style="margin-top:14px">
    <h3>Strategy Proposals <span style="color:var(--muted);font-weight:normal;font-size:.63rem">generated by research agent</span></h3>
    <div id="res-proposals"><div class="no-data">No proposals yet — generated automatically from high-relevance research findings.</div></div>
  </div>

  <!-- News Feed -->
  <div class="section" style="margin-bottom:14px;margin-top:14px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <h3>News Feed</h3>
      <span id="news-status" style="font-size:.65rem;color:var(--muted)">--</span>
    </div>
    <div id="news-list" style="display:flex;flex-direction:column;gap:6px">
      <div style="color:var(--muted);font-size:.72rem">No news loaded yet</div>
    </div>
  </div>

  <!-- Active Hedges -->
  <div class="section" style="margin-bottom:14px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <h3>Active Hedges</h3>
      <span id="hedge-status-badge" style="font-size:.65rem;color:var(--muted)">--</span>
    </div>
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px">
      <span style="font-size:.72rem"><span style="color:var(--muted)">Count:</span> <span id="hedge-count" style="color:var(--accent2)">--</span></span>
      <span style="font-size:.72rem"><span style="color:var(--muted)">Enabled:</span> <span id="hedge-enabled" style="color:var(--yellow)">--</span></span>
    </div>
    <table>
      <thead><tr><th>Token ID</th><th>Details</th></tr></thead>
      <tbody id="hedge-tbody">
        <tr><td colspan="2" style="color:var(--muted);text-align:center">No active hedges</td></tr>
      </tbody>
    </table>
  </div>

  <!-- QuickResolution -->
  <div class="section">
    <h3>QuickResolution Activity</h3>
    <div id="qr-status" style="padding:10px;background:var(--bg);border-radius:8px;font-size:.75rem;color:var(--muted)">
      QuickResolution: <span id="qr-active-badge" style="font-weight:700;color:var(--muted)">UNKNOWN</span>
    </div>
  </div>

  <!-- Proposal code viewer modal -->
  <div id="proposal-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.88);z-index:1000;overflow:auto;padding:20px">
    <div style="max-width:800px;margin:0 auto;background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);padding:22px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <h3 id="modal-title" style="margin:0;color:var(--text)"></h3>
        <button onclick="closeProposalModal()" style="background:none;border:none;color:var(--text);font-size:1.2rem;cursor:pointer">&#x2715;</button>
      </div>
      <div id="modal-finding" style="font-size:.72rem;color:var(--muted);margin-bottom:12px;padding:8px;background:var(--bg);border-radius:4px"></div>
      <pre id="modal-code" style="background:var(--bg);padding:14px;border-radius:8px;overflow:auto;font-size:.7rem;color:var(--text);max-height:60vh;white-space:pre-wrap"></pre>
      <div style="margin-top:14px;display:flex;gap:10px;align-items:center">
        <button id="modal-deploy-btn" onclick="deployProposal()" style="padding:8px 20px;background:var(--accent);color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Deploy Strategy</button>
        <span id="modal-deploy-status" style="font-size:.72rem;color:var(--muted)"></span>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB 6: META-AGENT                                          -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page" id="tab-meta">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;flex:1;min-width:0">
      <div class="card"><div class="lbl">Analyses Run</div><div class="val blue" id="meta-count">--</div></div>
      <div class="card"><div class="lbl">Last Run</div><div class="val" id="meta-last">--</div></div>
      <div class="card"><div class="lbl">Next Run</div><div class="val yellow" id="meta-next">--</div></div>
    </div>
    <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">
      <button id="ma-runnow-btn" onclick="runMetaAgentNow()" style="padding:7px 18px;background:#0a1a2a;color:#60a0ff;border:1px solid #1a3a5a;border-radius:8px;cursor:pointer;font-size:.75rem;font-weight:700;white-space:nowrap">Run Now</button>
      <span id="ma-runnow-status" style="font-size:.68rem;color:var(--muted)"></span>
    </div>
  </div>
  <div id="meta-latest-card">
    <div class="no-data">No meta-agent analysis yet.</div>
  </div>
  <div class="section" style="margin-top:14px">
    <h3>Analysis History</h3>
    <div id="meta-history"></div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB 7: SYSTEM  (was Status + Balances + Code Review + Compare) -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page" id="tab-system">

  <!-- Connections + API keys checklist -->
  <div class="section">
    <h3>Connections &amp; API Keys</h3>
    <div class="status-grid" id="status-connections">
      <div class="no-data">Loading...</div>
    </div>
  </div>

  <!-- AI Research integrations -->
  <div class="section">
    <h3>AI Research Integrations</h3>
    <div id="status-ai-research"><div class="no-data">Loading...</div></div>
  </div>

  <!-- Disk + costs -->
  <div class="section">
    <h3>Disk Usage</h3>
    <div id="status-disk"><div class="no-data">Loading...</div></div>
  </div>

  <!-- Kalshi diagnostics -->
  <div class="section">
    <h3>Kalshi / Cross-Exchange</h3>
    <div id="kalshi-diag"><div class="no-data">Loading...</div></div>
  </div>

  <!-- Billing cycle bar -->
  <div class="cycle-bar" style="margin-top:14px">
    <h3>Billing Cycle</h3>
    <div id="cycle-dates" style="display:flex;justify-content:space-between;font-size:.72rem;color:var(--muted);margin-bottom:8px">
      <span id="cycle-start">--</span><span id="cycle-days-left" style="color:var(--text)">-- days remaining</span><span id="cycle-end">--</span>
    </div>
    <div class="cycle-progress"><div class="cycle-fill" id="cycle-fill" style="width:0%"></div></div>
    <div style="font-size:.63rem;color:var(--muted);margin-top:5px;text-align:center"><span id="cycle-pct">0</span>% of billing cycle elapsed</div>
  </div>

  <!-- Service cost cards -->
  <div class="bal-grid">
    <div class="bal-card anthropic">
      <h3>Anthropic API</h3>
      <div class="bal-row"><span class="bal-lbl">Model</span><span class="bal-val" id="bal-ant-model">--</span></div>
      <div class="bal-row"><span class="bal-lbl">API Key</span><span class="bal-val" id="bal-ant-key">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Meta-Agent Runs</span><span class="bal-val" id="bal-ant-runs">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Est. Cost / Run</span><span class="bal-val" id="bal-ant-cpr">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Est. Spend This Deploy</span><span class="bal-val" id="bal-ant-cost" style="color:#a78bfa">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Projected Monthly</span><span class="bal-val" id="bal-ant-proj">--</span></div>
      <div class="bal-row" id="bal-ant-budget-row"><span class="bal-lbl">Monthly Budget</span><span class="bal-val" id="bal-ant-budget">Not set</span></div>
      <div class="budget-bar"><div class="budget-fill" id="bal-ant-bar" style="width:0%;background:#a78bfa"></div></div>
      <div class="budget-label"><span id="bal-ant-bar-lbl">Set ANTHROPIC_MONTHLY_BUDGET to track</span><span id="bal-ant-bar-pct"></span></div>
      <a href="https://console.anthropic.com/settings/billing" target="_blank" class="refill-link">+ Add Anthropic Credits</a>
    </div>

    <div class="bal-card railway">
      <h3>Railway</h3>
      <div class="bal-row"><span class="bal-lbl">Plan Base Cost</span><span class="bal-val" id="bal-rail-base">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Days Left in Cycle</span><span class="bal-val" id="bal-rail-days">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Disk Used / Limit</span><span class="bal-val" id="bal-rail-disk">--</span></div>
      <div class="budget-bar"><div class="budget-fill" id="bal-rail-bar" style="width:0%;background:var(--accent2)"></div></div>
      <div class="budget-label"><span>Disk usage</span><span id="bal-rail-bar-pct"></span></div>
      <div style="font-size:.66rem;color:var(--muted);margin-top:10px">Live billing data must be checked directly on Railway.</div>
      <a href="https://railway.app/account/billing" target="_blank" class="refill-link">View Billing on Railway</a>
    </div>

    <div class="bal-card bot">
      <h3>Bot Runtime</h3>
      <div class="bal-row"><span class="bal-lbl">Mode</span><span class="bal-val" id="bal-bot-mode">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Uptime This Deploy</span><span class="bal-val" id="bal-bot-uptime">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Trades Executed</span><span class="bal-val" id="bal-bot-trades">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Portfolio P&amp;L</span><span class="bal-val" id="bal-bot-pnl">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Cycles Left (billing)</span><span class="bal-val" id="bal-bot-cycles">--</span></div>
      <div class="bal-row"><span class="bal-lbl">Meta-Runs Left (billing)</span><span class="bal-val" id="bal-bot-metaruns">--</span></div>
      <a href="https://polymarket.com/wallet" target="_blank" class="refill-link">+ Deposit USDC to Polymarket</a>
    </div>
  </div>

  <!-- Code Review -->
  <div class="section" style="margin-top:14px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
      <h3 style="margin:0">Code Review</h3>
      <button id="cr-runnow-btn" onclick="runCodeReviewNow()" style="padding:5px 14px;background:#0a2a1a;color:var(--green);border:1px solid #1a4a2a;border-radius:8px;cursor:pointer;font-size:.72rem;font-weight:700">Run Now</button>
      <span id="cr-runnow-status" style="font-size:.7rem;color:var(--muted)"></span>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px">
      <div class="card"><div class="lbl">Code Grade</div><div class="val blue" id="cr-grade">--</div></div>
      <div class="card"><div class="lbl">Health Score</div><div class="val" id="cr-score">--</div></div>
      <div class="card"><div class="lbl">Last Review</div><div class="val yellow" id="cr-date">--</div></div>
      <div class="card"><div class="lbl">Findings</div><div class="val" id="cr-total">--</div><div class="sub" id="cr-severity">--</div></div>
    </div>

    <div class="meta-card" id="cr-summary-card">
      <h3>Summary</h3>
      <div id="cr-summary" class="meta-analysis">No review yet — runs automatically once a week.</div>
    </div>

    <div class="meta-card" id="cr-strengths-card" style="display:none">
      <h3>Strengths</h3>
      <div id="cr-strengths"></div>
    </div>

    <div style="margin-top:10px">
      <h3 style="font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px">Findings</h3>
      <div id="cr-findings"><div class="no-data">No findings yet.</div></div>
    </div>

    <div style="margin-top:14px">
      <h3 style="font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px">Review History</h3>
      <div id="cr-history"><div class="no-data">No history yet.</div></div>
    </div>

    <!-- Auto-Fix Panel -->
    <div class="meta-card" style="margin-top:14px" id="cr-autofix-card">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <h3 style="margin:0">Auto-Fix with Claude</h3>
        <button id="cr-autofix-btn" onclick="triggerAutofix()" style="padding:5px 14px;background:#0a1a3a;color:var(--accent2);border:1px solid #1a2a5a;border-radius:8px;cursor:pointer;font-size:.72rem;font-weight:700">
          Run Auto-Fix
        </button>
        <span id="cr-autofix-status" style="font-size:.7rem;color:var(--muted)"></span>
      </div>
      <div style="font-size:.66rem;color:var(--muted);margin-top:5px">
        Sends each high &amp; medium finding to Claude Sonnet for targeted edits with syntax validation.
      </div>
      <div id="cr-autofix-results" style="margin-top:10px"></div>
    </div>
  </div>

  <!-- Bot Comparison -->
  <div class="section" style="margin-top:14px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3>Bot Comparison</h3>
      <span id="compare-ts" style="font-size:.65rem;color:var(--muted)">--</span>
    </div>

    <div id="compare-b-warning" style="display:none;background:#2a1a00;border:1px solid #4a3000;border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:.75rem;color:var(--yellow)"></div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div style="background:var(--surface2);border:1px solid var(--border);border-top:3px solid var(--green);border-radius:var(--card-radius);padding:16px">
        <div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:var(--green);margin-bottom:12px;font-weight:700" id="compare-a-name">Bot A</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Balance</span><span id="compare-a-bal">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Total PnL</span><span id="compare-a-pnl">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Realized PnL</span><span id="compare-a-realized">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">ROI</span><span id="compare-a-roi">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Win Rate</span><span id="compare-a-wr">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Fees Paid</span><span id="compare-a-fees" style="color:var(--red)">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Exposure</span><span id="compare-a-exp">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Open Positions</span><span id="compare-a-pos">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Total Trades</span><span id="compare-a-trades">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Trades/hr</span><span id="compare-a-tph">--</span></div>
        </div>
      </div>
      <div style="background:var(--surface2);border:1px solid var(--border);border-top:3px solid #818cf8;border-radius:var(--card-radius);padding:16px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:#818cf8;font-weight:700" id="compare-b-name">Bot B</div>
          <span id="compare-b-fresh-badge" style="display:none;font-size:.63rem;background:#1a1a00;color:var(--yellow);border:1px solid var(--yellow);border-radius:4px;padding:1px 6px">Just started</span>
          <span id="compare-b-noconfig-badge" style="display:none;font-size:.63rem;background:#1a0000;color:var(--red);border:1px solid var(--red);border-radius:4px;padding:1px 6px">Not configured</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:8px">
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Balance</span><span id="compare-b-bal">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Total PnL</span><span id="compare-b-pnl">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Realized PnL</span><span id="compare-b-realized">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">ROI</span><span id="compare-b-roi">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Win Rate</span><span id="compare-b-wr">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Fees Paid</span><span id="compare-b-fees" style="color:var(--red)">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Exposure</span><span id="compare-b-exp">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Open Positions</span><span id="compare-b-pos">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Total Trades</span><span id="compare-b-trades">--</span></div>
          <div style="display:flex;justify-content:space-between;font-size:.76rem"><span style="color:var(--muted)">Trades/hr</span><span id="compare-b-tph">--</span></div>
        </div>
      </div>
    </div>

    <h3 style="font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px">Strategy Breakdown</h3>
    <table>
      <thead><tr>
        <th>Strategy</th>
        <th style="text-align:right">A PnL</th><th style="text-align:right">A Trades</th>
        <th style="text-align:right">B PnL</th><th style="text-align:right">B Trades</th>
      </tr></thead>
      <tbody id="compare-strat-tbody">
        <tr><td colspan="5" style="color:var(--muted);text-align:center">No data yet</td></tr>
      </tbody>
    </table>

    <div id="compare-setup" style="display:none;margin-top:20px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--card-radius);padding:16px;font-size:.75rem;color:var(--muted);line-height:1.7">
      <div style="color:var(--yellow);font-weight:700;margin-bottom:8px">Setup required</div>
      Set the <code style="background:var(--bg);padding:2px 6px;border-radius:4px;color:var(--accent2)">PEER_BOT_URL</code> environment variable to Bot B's dashboard URL and restart.
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB 8: WEATHER                                             -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page" id="tab-weather">

  <!-- Summary cards -->
  <div class="kpi-row" id="weather-kpi-row" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
    <div class="kpi-card"><div class="kpi-label">Total Trades</div><div class="kpi-value" id="w-total">--</div></div>
    <div class="kpi-card"><div class="kpi-label">Wins</div><div class="kpi-value" id="w-wins">--</div></div>
    <div class="kpi-card"><div class="kpi-label">Win Rate</div><div class="kpi-value" id="w-winrate">--</div></div>
    <div class="kpi-card"><div class="kpi-label">Total PnL</div><div class="kpi-value" id="w-pnl">--</div></div>
  </div>

  <!-- Per-city breakdown -->
  <div class="section">
    <h3>Per-City Breakdown</h3>
    <table class="trade-table">
      <thead><tr>
        <th>City</th>
        <th style="text-align:right">Trades</th>
        <th style="text-align:right">Win Rate</th>
        <th style="text-align:right">PnL</th>
      </tr></thead>
      <tbody id="w-city-tbody">
        <tr><td colspan="4" style="color:var(--muted);text-align:center">No weather trades yet</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Recent signals -->
  <div class="section">
    <h3>Recent Signals</h3>
    <table class="trade-table">
      <thead><tr>
        <th>Date</th>
        <th>City</th>
        <th style="text-align:right">Model</th>
        <th style="text-align:right">Market</th>
        <th style="text-align:right">Edge</th>
        <th>Side</th>
        <th style="text-align:right">PnL</th>
      </tr></thead>
      <tbody id="w-signals-tbody">
        <tr><td colspan="7" style="color:var(--muted);text-align:center">No signals yet</td></tr>
      </tbody>
    </table>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- TAB: SPORTS                                                -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="page" id="tab-sports">

  <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
    <h2 style="font-size:.9rem;font-weight:700;color:var(--text);margin:0">Live Sports</h2>
    <span style="font-size:.65rem;color:var(--muted)">ESPN scores · refreshes every 30s · no API cost</span>
    <button onclick="loadSports();loadSportsLive();" style="margin-left:auto;font-size:.65rem;padding:4px 10px;background:var(--surface2);color:var(--muted);border:1px solid var(--border);border-radius:6px;cursor:pointer">Refresh</button>
  </div>

  <!-- KPI row -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
    <div class="kpi-card"><div class="kpi-label">Sports Bets</div><div class="kpi-value" id="sp-total">--</div></div>
    <div class="kpi-card">
      <div class="kpi-label">Win Rate</div>
      <div class="kpi-value" id="sp-winrate">--</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Total PnL</div>
      <div class="kpi-value" id="sp-pnl">--</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Open Positions</div>
      <div class="kpi-value" id="sp-open">--</div>
    </div>
  </div>

  <!-- Live games (ESPN) -->
  <div class="section">
    <h3>Live Games <span id="sp-live-badge" style="font-size:.7rem;background:var(--red);color:#fff;padding:2px 8px;border-radius:4px;vertical-align:middle;display:none">LIVE</span></h3>
    <div id="sp-live-games"><div class="no-data" style="color:var(--muted)">Loading live games...</div></div>
  </div>

  <!-- Open positions with player stats -->
  <div class="section">
    <h3>Open Sports Positions</h3>
    <div id="sp-open-positions"><div class="no-data" style="color:var(--muted)">No open sports positions</div></div>
  </div>

  <!-- Bet type breakdown -->
  <div class="section">
    <h3>Performance by Bet Type</h3>
    <table class="trade-table">
      <thead><tr>
        <th>Bet Type</th>
        <th style="text-align:right">Trades</th>
        <th style="text-align:right">Win Rate</th>
        <th style="text-align:right">PnL</th>
      </tr></thead>
      <tbody id="sp-bettype-tbody">
        <tr><td colspan="4" style="color:var(--muted);text-align:center">No data yet</td></tr>
      </tbody>
    </table>
  </div>

  <!-- League breakdown -->
  <div class="section">
    <h3>Performance by League</h3>
    <table class="trade-table">
      <thead><tr>
        <th>League</th>
        <th style="text-align:right">Trades</th>
        <th style="text-align:right">Win Rate</th>
        <th style="text-align:right">PnL</th>
      </tr></thead>
      <tbody id="sp-league-tbody">
        <tr><td colspan="4" style="color:var(--muted);text-align:center">No data yet</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Recent sports trades -->
  <div class="section">
    <h3>Recent Sports Trades</h3>
    <table class="trade-table">
      <thead><tr>
        <th>Market</th>
        <th>Type</th>
        <th>League</th>
        <th>Outcome</th>
        <th style="text-align:right">PnL</th>
        <th>Result</th>
      </tr></thead>
      <tbody id="sp-recent-tbody">
        <tr><td colspan="6" style="color:var(--muted);text-align:center">No trades yet</td></tr>
      </tbody>
    </table>
  </div>

</div>

<!-- Hidden elements required by log stream (live-uptime, live-realized, etc.) -->
<div style="display:none">
  <span id="live-uptime">--</span>
  <span id="live-realized">--</span>
  <span id="live-winrate">--</span>
  <span id="live-trades">--</span>
  <div id="log-feed"></div>
  <input type="checkbox" id="autoscroll" checked>
</div>

<div id="last-update">--</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
const $=id=>document.getElementById(id);
const fmt=(n,d=2)=>n==null?'--':'$'+Number(n).toFixed(d).replace(/\B(?=(\d{3})+(?!\d))/g,',');
const fmtPnl=n=>{if(n==null)return'--';const s=n>=0?'+':'-';return s+'$'+Math.abs(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g,',')};
const fmtN=n=>n==null?'--':Number(n).toFixed(4);
const ts=t=>new Date(t*1000).toLocaleTimeString();
const tsDate=t=>new Date(t*1000).toLocaleString();
const badge=s=>`<span class="badge ${s}">${s}</span>`;
const pnlClass=n=>n>=0?'green':'red';

let currentTab='overview';
let _statusInterval=null;

// 9 tabs: overview, positions, trades, strategies, ai-intel, meta, system, weather, sports
const allTabs=['overview','positions','trades','strategies','ai-intel','meta','system','weather','sports'];
let _ensembleInterval=null,_newsInterval=null,_hedgeInterval=null,_compareInterval=null;
let _sportsInterval=null;

function showTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>{t.classList.toggle('active',allTabs[i]===name)});
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  const pg=$('tab-'+name);
  if(pg) pg.classList.add('active');
  currentTab=name;

  if(name==='system'){
    fetchSystemStatus();
    fetchBalances();
    fetchCodeReview();
    fetchCompare();
    if(_statusInterval) clearInterval(_statusInterval);
    _statusInterval=setInterval(fetchSystemStatus,10000);
    if(_compareInterval)clearInterval(_compareInterval);
    _compareInterval=setInterval(fetchCompare,30000);
  } else {
    if(_statusInterval){clearInterval(_statusInterval);_statusInterval=null;}
    if(_compareInterval){clearInterval(_compareInterval);_compareInterval=null;}
  }

  if(name==='strategies'){
    fetchAnalytics();
    fetchSystemStatus();
  }

  if(name==='weather'){
    loadWeather();
  }

  if(name==='sports'){
    loadSports();
    loadSportsLive();
    if(window._sportsInterval) clearInterval(window._sportsInterval);
    window._sportsInterval=setInterval(()=>{loadSports();loadSportsLive();},30000);
  } else {
    if(window._sportsInterval){clearInterval(window._sportsInterval);window._sportsInterval=null;}
  }

  if(name==='ai-intel'){
    fetchEnsemble();
    fetchNews();
    fetchHedges();
    fetchResearch();
    if(_ensembleInterval)clearInterval(_ensembleInterval);
    if(_newsInterval)clearInterval(_newsInterval);
    if(_hedgeInterval)clearInterval(_hedgeInterval);
    _ensembleInterval=setInterval(fetchEnsemble,60000);
    _newsInterval=setInterval(fetchNews,120000);
    _hedgeInterval=setInterval(fetchHedges,30000);
  } else {
    if(_ensembleInterval){clearInterval(_ensembleInterval);_ensembleInterval=null;}
    if(_newsInterval){clearInterval(_newsInterval);_newsInterval=null;}
    if(_hedgeInterval){clearInterval(_hedgeInterval);_hedgeInterval=null;}
  }

}

function loadWeather(){
  fetch('/api/weather/stats').then(r=>r.json()).then(d=>{
    $('w-total').textContent=d.total_trades;
    $('w-wins').textContent=d.wins;
    $('w-winrate').textContent=d.win_rate+'%';
    const pEl=$('w-pnl');
    pEl.textContent=fmtPnl(d.total_pnl);
    pEl.className='kpi-value '+(d.total_pnl>=0?'green':'red');

    // City table
    const ctb=$('w-city-tbody');
    if(d.cities&&d.cities.length){
      ctb.innerHTML=d.cities.map(c=>`
        <tr>
          <td>${c.city}</td>
          <td style="text-align:right">${c.trades}</td>
          <td style="text-align:right">${c.win_rate}%</td>
          <td style="text-align:right;color:${c.pnl>=0?'var(--green)':'var(--red)'}">${fmtPnl(c.pnl)}</td>
        </tr>`).join('');
    } else {
      ctb.innerHTML='<tr><td colspan="4" style="color:var(--muted);text-align:center">No weather trades yet</td></tr>';
    }

    // Signals table
    const stb=$('w-signals-tbody');
    if(d.recent_signals&&d.recent_signals.length){
      stb.innerHTML=d.recent_signals.map(s=>`
        <tr>
          <td>${s.date}</td>
          <td>${s.city}</td>
          <td style="text-align:right">${s.model_prob}%</td>
          <td style="text-align:right">${(s.market_price*100).toFixed(1)}%</td>
          <td style="text-align:right">${s.edge}%</td>
          <td><span class="badge ${s.side==='BUY'?'green':'yellow'}">${s.side}</span></td>
          <td style="text-align:right;color:${s.pnl>=0?'var(--green)':'var(--red)'}">${fmtPnl(s.pnl)}</td>
        </tr>`).join('');
    } else {
      stb.innerHTML='<tr><td colspan="7" style="color:var(--muted);text-align:center">No signals yet</td></tr>';
    }
  }).catch(e=>console.error('weather stats error',e));
}

const chartDefaults={responsive:true,maintainAspectRatio:true,plugins:{legend:{display:false}},scales:{x:{display:false,grid:{color:'#1a1a1a'}},y:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}}}};

const pnlCtx=$('pnlChart').getContext('2d');
const pnlChart=new Chart(pnlCtx,{type:'line',data:{labels:[],datasets:[{label:'Portfolio Value',data:[],borderColor:'#00e5ff',backgroundColor:'rgba(0,229,255,.05)',borderWidth:1.5,pointRadius:0,fill:true,tension:.3},{label:'Realized P&L',data:[],borderColor:'#00e676',backgroundColor:'transparent',borderWidth:1.5,pointRadius:0,tension:.3}]},options:{...chartDefaults,plugins:{legend:{display:true,labels:{color:'#666',font:{size:10},boxWidth:10}}}}});

const stratCtx=$('stratChart').getContext('2d');
const stratChart=new Chart(stratCtx,{type:'doughnut',data:{labels:[],datasets:[{data:[],backgroundColor:['#00e676','#7986cb','#ff7043','#ffd740','#4dd0e1','#ce93d8'],borderColor:'#0a0a0a',borderWidth:2}]},options:{responsive:true,maintainAspectRatio:true,plugins:{legend:{position:'right',labels:{color:'#666',font:{size:10},boxWidth:10}}}}});

// Analytics charts (lazy-init)
let roiChart=null,winRateChart=null,hourlyPnlChart=null,feeDragChart=null,healthTrendChart=null;

function getOrCreateChart(id,config){
  const ctx=$(id).getContext('2d');
  return new Chart(ctx,config);
}

function updatePnlChart(history){
  if(!history.length)return;
  const step=Math.max(1,Math.floor(history.length/150));
  const sampled=history.filter((_,i)=>i%step===0||i===history.length-1);
  pnlChart.data.labels=sampled.map(p=>ts(p.t));
  pnlChart.data.datasets[0].data=sampled.map(p=>p.value);
  pnlChart.data.datasets[1].data=sampled.map(p=>p.pnl);
  pnlChart.update('none');
}

function updateStratChart(counts){
  const entries=Object.entries(counts);
  stratChart.data.labels=entries.map(([k])=>k);
  stratChart.data.datasets[0].data=entries.map(([,v])=>v);
  stratChart.update('none');
}

async function fetchAll(){
  try{
    const [status,pnlH,stratPnl,stratTrades]=await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/pnl_history').then(r=>r.json()),
      fetch('/api/strategy_pnl').then(r=>r.json()),
      fetch('/api/strategy_trades').then(r=>r.json()),
    ]);
    updateStatus(status);
    updatePnlChart(pnlH);
    updateStratPnl(stratPnl);
    updateStratChart(stratTrades);

    if(currentTab==='positions'){
      const [open,closed]=await Promise.all([
        fetch('/api/positions').then(r=>r.json()),
        fetch('/api/closed_positions').then(r=>r.json()),
      ]);
      updatePositions(open,closed);
    }
    if(currentTab==='trades'){const d=await fetch('/api/trades').then(r=>r.json());updateTrades(d,status);}
    if(currentTab==='meta'){fetchMeta();}

    $('last-update').textContent='Updated: '+new Date().toLocaleTimeString();
  }catch(e){$('last-update').textContent='Connection error...';}
}

function updateStatus(s){
  $('uptime-info').textContent='Uptime: '+s.uptime+' | Cycles: '+s.cycle_count;
  $('balance').textContent=fmt(s.balance);

  const tv=s.total_value||0,tp=s.pnl||0,tpp=s.pnl_pct||0;
  // Overview: show total P&L as the big number, not total value
  $('total-value').textContent=fmtPnl(tp);
  $('total-value').className='val '+(tp>=0?'green':'red');
  $('total-pnl-sub').textContent=fmt(tv)+' total value · '+(tpp>=0?'+':'')+tpp.toFixed(2)+'%';
  $('total-pnl-sub').style.color=tp>=0?'var(--green)':'var(--red)';

  const rp=s.realized_pnl||0,rpp=s.realized_pnl_pct||0;
  $('realized-pnl').textContent=fmtPnl(rp);
  $('realized-pnl').className='val '+(rp>=0?'green':'red');
  $('realized-pnl-pct').innerHTML=(rpp>=0?'+':'')+rpp.toFixed(2)+'% | <span id="closed-count">'+s.closed_positions+'</span> closed';

  const wr=s.win_rate||0;
  $('win-rate').textContent=wr.toFixed(1)+'%';
  $('win-rate').className='val '+(wr>=50?'green':'yellow');

  $('pos-count').textContent=s.open_positions;
  $('exposure-sub').textContent='Exposure: '+fmt(s.exposure);

  $('trades-per-hr').textContent=s.trades_per_hour||'--';
  $('total-trades-sub').textContent=(s.total_trades||0)+' total';

  $('fees').textContent=fmt(s.fees_paid);

  // Cycle count card
  const lc=$('live-cycle');
  if(lc) lc.textContent=s.cycle_count;
  const lu=$('live-uptime-sub');
  if(lu) lu.textContent=s.uptime;

  // Hidden live-tab elements (kept for log-stream compatibility)
  const liveu=$('live-uptime');
  if(liveu) liveu.textContent=s.uptime;
  const liver=$('live-realized');
  if(liver){liver.textContent=fmtPnl(rp);liver.className='val '+(rp>=0?'green':'red');}
  const livew=$('live-winrate');
  if(livew){livew.textContent=wr.toFixed(1)+'%';livew.className='val '+(wr>=50?'green':'red');}
  const livet=$('live-trades');
  if(livet) livet.textContent=s.total_trades;

  // Polymarket public profile + activity (funder / proxy wallet from env)
  const addr=(s.polymarket_address||'').trim();
  const profileUrl=addr?`https://polymarket.com/profile/${addr}`:'https://polymarket.com/';
  const el=$('poly-profile-link');
  if(el){el.href=profileUrl;el.title=addr?`Wallet: ${addr}`:'Set POLYMARKET_FUNDER_ADDRESS for your profile link';}
  const el2=$('poly-portfolio-link');
  if(el2){
    el2.href=addr?`https://polymarket.com/profile/${addr}`:'https://polymarket.com/portfolio';
    el2.title=addr?'Open your Polymarket profile (positions & history)':'Log in at polymarket.com/portfolio';
  }
}

function updateStratPnl(data){
  const entries=Object.entries(data);
  if(!entries.length){$('strat-bars').innerHTML='<div class="no-data">Waiting for trades...</div>';return;}
  const max=Math.max(...entries.map(([,v])=>Math.abs(v)),1);
  $('strat-bars').innerHTML=entries.sort((a,b)=>b[1]-a[1]).map(([name,val])=>{
    const pct=Math.abs(val)/max*100,cls=val>=0?'pos':'neg',sign=val>=0?'+':'';
    return`<div class="strat-row"><div class="name">${name}</div><div class="bar-wrap"><div class="bar ${cls}" style="width:${Math.max(pct,5)}%">${sign}$${val.toFixed(2)}</div></div></div>`;
  }).join('');
}

async function closePosition(idx){
  if(!confirm(`Force-close position #${idx}?\n\nThis sells at near-zero price. Use only for stuck/worthless positions.`)) return;
  const priceStr = prompt('Exit price (0.001 for worthless, or enter current bid):', '0.001');
  if(priceStr===null) return;
  const price = parseFloat(priceStr);
  if(isNaN(price)||price<0||price>1){alert('Invalid price. Must be between 0 and 1.');return;}
  try{
    const r=await fetch(`/api/positions/close?idx=${idx}&price=${price}`,{method:'POST'});
    const d=await r.json();
    if(d.ok){
      alert(`Closed ${d.contracts} contracts @ ${d.price}. Position removed.`);
    }else{
      alert('Error: '+(d.error||'Unknown error'));
    }
  }catch(e){alert('Request failed: '+e);}
}

function updatePositions(open,closed){
  $('open-pos-count').textContent=open.length;
  if(!open.length){
    $('positions-table').innerHTML='<div class="no-data">No open positions</div>';
  }else{
    const now=Math.floor(Date.now()/1000);
    $('positions-table').innerHTML=`<table>
      <tr><th>Market</th><th>Outcome</th><th>Contracts</th><th>Avg Cost</th><th>Cost Basis</th><th>Strategy</th><th>Age</th><th>Est. Close</th><th></th></tr>
      ${open.map((p,i)=>{
        const ageSec=now-p.opened_at;
        const ageDays=ageSec/86400;
        const ageStr=ageDays>=1?Math.floor(ageDays)+'d '+Math.floor((ageSec%86400)/3600)+'h':Math.floor(ageSec/3600)+'h '+Math.floor((ageSec%3600)/60)+'m';
        const stale=ageDays>7;
        let estClose='—';
        if(p.end_date_iso){
          const d=new Date(p.end_date_iso);
          const dSec=d.getTime()/1000;
          if(dSec>now){
            const rem=dSec-now;
            const remDays=rem/86400;
            estClose=remDays>=1?Math.floor(remDays)+'d '+Math.floor((rem%86400)/3600)+'h':Math.floor(rem/3600)+'h '+Math.floor((rem%3600)/60)+'m';
          } else if(p.market_active!==false){
            // end_date passed but Polymarket still marks the market active — game in progress
            estClose='<span style="color:var(--green)">live</span>';
          } else {
            estClose='<span style="color:var(--red)">overdue</span>';
          }
        }
        return`<tr style="${stale?'background:#1a0a0a':''}">
          <td title="${p.question}">${p.question||'<em style="color:var(--muted)">unknown market</em>'}</td>
          <td>${p.outcome}</td>
          <td>${p.contracts}</td>
          <td>${fmtN(p.avg_cost)}</td>
          <td>${fmt(p.cost_basis)}</td>
          <td>${badge(p.strategy)}</td>
          <td class="ts-small" style="${stale?'color:var(--red)':''}" title="Opened ${ts(p.opened_at)}">${ageStr}${stale?' ⚠':''}</td>
          <td class="ts-small">${estClose}</td>
          <td><button onclick="closePosition(${i})" style="font-size:.7rem;padding:3px 8px;background:#2a0a0a;color:#f87171;border:1px solid #7f1d1d;border-radius:4px;cursor:pointer">Close</button></td>
        </tr>`;
      }).join('')}
    </table>`;
  }

  if(!closed.length){
    $('closed-table').innerHTML='<div class="no-data">No closed positions yet — positions close when fully sold</div>';
  }else{
    const totalR=closed.reduce((s,p)=>s+p.realized_pnl,0);
    const wins=closed.filter(p=>p.realized_pnl>0).length;
    $('closed-table').innerHTML=`
      <div style="display:flex;gap:20px;margin-bottom:10px;font-size:.78rem">
        <span>Total Realized: <strong class="${totalR>=0?'win':'loss'}">${fmtPnl(totalR)}</strong></span>
        <span>Win Rate: <strong class="${wins/closed.length>=.5?'win':'loss'}">${(wins/closed.length*100).toFixed(1)}%</strong></span>
        <span style="color:#555">(${wins}W / ${closed.length-wins}L of ${closed.length} closed)</span>
      </div>
      <table>
        <tr><th>Market</th><th>Outcome</th><th>Strategy</th><th>Realized P&L</th><th>Result</th><th>Closed</th><th>Duration</th></tr>
        ${closed.map(p=>{
          const dur=Math.round((p.closed_at-p.opened_at)/60);
          const durStr=dur<60?dur+'m':Math.round(dur/60)+'h '+dur%60+'m';
          const isWin=p.realized_pnl>0;
          return`<tr>
            <td title="${p.market_question||''}">${(p.market_question||'').slice(0,55)}</td>
            <td>${p.outcome||''}</td>
            <td>${badge(p.strategy)}</td>
            <td class="${isWin?'win':'loss'}">${fmtPnl(p.realized_pnl)}</td>
            <td><span style="color:${isWin?'#00e676':'#ff5252'};font-weight:700">${isWin?'WIN':'LOSS'}</span></td>
            <td class="ts-small">${ts(p.closed_at)}</td>
            <td class="ts-small">${durStr}</td>
          </tr>`;
        }).join('')}
      </table>`;
  }
}

function updateTrades(data,status){
  const buys=data.filter(t=>t.side==='BUY').length;
  $('t-total').textContent=status.total_trades;
  $('t-buys').textContent=buys;
  $('t-sells').textContent=data.length-buys;
  $('t-fees').textContent=fmt(status.fees_paid);
  if(!data.length){$('trades-table').innerHTML='<div class="no-data">No trades yet</div>';return;}
  $('trades-table').innerHTML=`<table>
    <tr><th>ID</th><th>Time</th><th>Strategy</th><th>Token (CLOB)</th><th>Side</th><th>Contracts</th><th>Price</th><th>Amount</th><th>Realized P&amp;L</th><th>Notes</th></tr>
    ${data.map(t=>{
      const rp=t.realized_pnl||0;
      const rpCell=t.side==='SELL'
        ?`<td class="${rp>=0?'win':'loss'}" style="font-weight:600">${rp>=0?'+':''}${fmt(rp)}</td>`
        :`<td style="color:#555">—</td>`;
      return`<tr>
      <td>${t.trade_id}</td>
      <td class="ts-small">${ts(t.timestamp)}</td>
      <td>${badge(t.strategy)}</td>
      <td class="mono" style="font-size:.65rem;max-width:120px;overflow:hidden;text-overflow:ellipsis" title="${(t.token_id_full||'').replace(/"/g,'')}">${t.token_id||'—'}</td>
      <td class="${t.side.toLowerCase()}">${t.side}</td>
      <td>${t.contracts}</td>
      <td>${fmtN(t.price)}</td>
      <td>${fmt(t.usdc_amount)}</td>
      ${rpCell}
      <td style="color:#555">${t.notes}</td>
    </tr>`;}).join('')}
  </table>`;
}

// ------------------------------------------------------------------ //
//  Status tab                                                          //
// ------------------------------------------------------------------ //
async function fetchSystemStatus(){
  const [sysRes,kalshiRes]=await Promise.allSettled([
    fetch('/api/system').then(r=>{if(!r.ok)throw new Error(r.status);return r.json();}),
    fetch('/api/kalshi/status').then(r=>{if(!r.ok)throw new Error(r.status);return r.json();}),
  ]);
  if(sysRes.status==='fulfilled'){
    try{renderSystemStatus(sysRes.value);}catch(e){console.error('renderSystemStatus error',e);}
  }else{
    const el=$('status-connections');
    if(el) el.innerHTML='<div class="no-data">Failed to load system status: '+sysRes.reason+'</div>';
  }
  if(kalshiRes.status==='fulfilled'){
    try{renderKalshiDiag(kalshiRes.value);}catch(e){console.error('renderKalshiDiag error',e);}
  }else{
    const el=$('kalshi-diag');
    if(el) el.innerHTML='<div class="no-data" style="color:var(--muted)">Kalshi status unavailable: '+kalshiRes.reason+'</div>';
  }
}

function renderSystemStatus(d){
  // Mode badge in header
  const modeBadge=$('mode-badge');
  modeBadge.textContent=d.mode||'PAPER';
  modeBadge.style.background=d.mode==='LIVE'?'#2a0a0a':'#0e1a2a';
  modeBadge.style.color=d.mode==='LIVE'?'var(--red)':'var(--accent2)';

  // Connections section
  const connItems=[
    {key:'polymarket',label:'Polymarket API'},
    {key:'binance',label:'Binance WebSocket'},
    {key:'kalshi',label:'Kalshi'},
  ];
  const apiItems=[
    {key:'anthropic',label:'Anthropic API Key'},
    {key:'polymarket',label:'Polymarket Key'},
    {key:'kalshi_rsa',label:'Kalshi RSA Key'},
    {key:'kalshi_token',label:'Kalshi Token'},
    {key:'perplexity',label:'Perplexity API Key'},
    {key:'grok',label:'Grok API Key'},
  ];

  let connHtml=connItems.map(({key,label})=>{
    const c=d.connections&&d.connections[key]||{status:'error',detail:''};
    return`<div class="status-item">
      <div class="dot ${c.status==='ok'?'ok':c.status==='warn'?'warn':'err'}"></div>
      <div><div class="status-label">${label}</div><div class="status-detail">${c.detail||''}</div></div>
    </div>`;
  }).join('');

  connHtml+=apiItems.map(({key,label})=>{
    const has=d.api_keys&&d.api_keys[key];
    return`<div class="status-item">
      <div class="dot ${has?'ok':'err'}"></div>
      <div><div class="status-label">${label}</div><div class="status-detail">${has?'Configured':'Not set'}</div></div>
    </div>`;
  }).join('');

  // Meta-agent connection item
  const ma=d.meta_agent||{};
  const maStatus=ma.enabled?'ok':'err';
  const maDetail=ma.enabled?(ma.last_run_ago_minutes!=null?'Last run '+ma.last_run_ago_minutes+'m ago':'Not run yet'):'No API key';
  connHtml+=`<div class="status-item">
    <div class="dot ${maStatus}"></div>
    <div><div class="status-label">Meta-Agent (Claude)</div><div class="status-detail">${maDetail} · every ${ma.interval_minutes||30}m</div></div>
  </div>`;

  const connEl=$('status-connections');
  if(connEl) connEl.innerHTML=connHtml;

  // Strategies section — rich cards with metrics
  const strats=d.strategies||{};
  // We need analytics data for per-strategy PnL/winrate — fetch async and merge
  const stratEl=$('status-strategies');
  if(stratEl){
    const stratHtml=Object.entries(strats).map(([name,info])=>{
      const cls=info.enabled?'enabled':'disabled';
      const statusTxt=info.enabled
        ?'<span class="strat-status" style="color:var(--green);font-size:.68rem;font-weight:700;background:#0a2a1a;padding:2px 8px;border-radius:10px">ENABLED</span>'
        :'<span class="strat-status" style="color:var(--red);font-size:.68rem;font-weight:700;background:#2a0a0a;padding:2px 8px;border-radius:10px">DISABLED</span>';
      return`<div class="strat-card ${cls}">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
          <h4>${name}</h4>${statusTxt}
        </div>
        <div class="strat-note">${info.note||''}</div>
        <div class="strat-metrics" id="sm-${name}">
          <div class="strat-metric"><span class="sm-lbl">P&amp;L</span><span class="sm-val" style="color:var(--muted)">--</span></div>
          <div class="strat-metric"><span class="sm-lbl">Trades</span><span class="sm-val" style="color:var(--muted)">--</span></div>
          <div class="strat-metric"><span class="sm-lbl">Win%</span><span class="sm-val" style="color:var(--muted)">--</span></div>
        </div>
      </div>`;
    }).join('');
    stratEl.innerHTML=stratHtml||'<div class="no-data">No strategy info</div>';

    // Async fetch analytics to fill strategy metrics
    fetch('/api/analytics').then(r=>r.json()).then(an=>{
      Object.keys(strats).forEach(name=>{
        const el=document.getElementById('sm-'+name);
        if(!el)return;
        const pnl=an.strategy_roi&&an.strategy_roi[name]!=null?an.strategy_roi[name]:null;
        const trades=an.strategy_trade_counts&&an.strategy_trade_counts[name]!=null?an.strategy_trade_counts[name]:null;
        const wr=an.strategy_win_rates&&an.strategy_win_rates[name]!=null?an.strategy_win_rates[name]:null;
        const spans=el.querySelectorAll('.sm-val');
        if(spans[0]&&pnl!=null){
          spans[0].textContent=(pnl>=0?'+':'')+pnl.toFixed(1)+'%';
          spans[0].style.color=pnl>=0?'var(--green)':'var(--red)';
        }
        if(spans[1]&&trades!=null){
          spans[1].textContent=trades;
          spans[1].style.color='var(--text)';
        }
        if(spans[2]&&wr!=null){
          spans[2].textContent=wr.toFixed(0)+'%';
          spans[2].style.color=wr>=50?'var(--green)':'var(--yellow)';
        }
      });
    }).catch(()=>{});

    // ── Optimism Tax panel ──────────────────────────────────────────────
    // Chart instances (keep refs so we can destroy/recreate on next poll)
    if(!window._otCharts) window._otCharts={};

    Promise.all([
      fetch('/api/optimism_tax/stats').then(r=>r.json()),
      fetch('/api/optimism_tax/model_viz').then(r=>r.json()),
    ]).then(([ot, viz])=>{
      const s=ot.summary||{};
      const setEl=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};

      // --- Summary strip ---
      setEl('ot-total',s.total_trades??'0');
      const edgeEl=document.getElementById('ot-edge');
      if(edgeEl){const e=s.avg_edge||0;edgeEl.textContent=(e*100).toFixed(2)+'pp';edgeEl.style.color=e>0?'#06b6d4':'var(--muted)';}
      const mcpEl=document.getElementById('ot-mcp');
      if(mcpEl){const p=s.avg_mc_p_profit||0;mcpEl.textContent=p?(p*100).toFixed(1)+'%':'--';mcpEl.style.color=p>=0.9?'var(--green)':p>=0.7?'var(--yellow)':'var(--red)';}
      const wrEl=document.getElementById('ot-wr');
      if(wrEl){wrEl.textContent=s.win_rate!=null?s.win_rate.toFixed(1)+'%':'--';wrEl.style.color=(s.win_rate||0)>=90?'var(--green)':(s.win_rate||0)>=70?'var(--yellow)':'var(--red)';}
      setEl('ot-wl',(s.wins||0)+' / '+(s.losses||0));
      const bi=document.getElementById('ot-block-info');
      if(bi&&viz.params){bi.textContent=`EDGE: ${((viz.params.f_star||0)*100).toFixed(1)}%  ·  YES@${((viz.params.yes_ask||0)*100).toFixed(1)}¢  ·  NO@${((viz.params.entry_price||0)*100).toFixed(1)}¢`;}

      // --- Bayesian chart ---
      const bayCtx=document.getElementById('ot-bayesian-chart');
      if(bayCtx&&viz.pdf){
        if(window._otCharts.bay){window._otCharts.bay.destroy();}
        const sub=document.getElementById('ot-bay-subtitle');
        if(sub&&viz.params){sub.textContent=`α=${viz.params.alpha.toFixed(1)}  β=${viz.params.beta.toFixed(1)}`;}
        window._otCharts.bay=new Chart(bayCtx,{
          type:'line',
          data:{
            labels:viz.pdf.xs,
            datasets:[
              {label:'Market',data:viz.pdf.market,borderColor:'#ef4444',borderWidth:1.5,pointRadius:0,fill:false,tension:.4,borderDash:[4,3]},
              {label:'Calibrated',data:viz.pdf.calibrated,borderColor:'#06b6d4',borderWidth:2,pointRadius:0,fill:true,backgroundColor:'rgba(6,182,212,.08)',tension:.4},
            ]
          },
          options:{
            responsive:true,maintainAspectRatio:true,
            plugins:{legend:{display:false},tooltip:{enabled:false}},
            scales:{
              x:{display:true,ticks:{color:'#334155',font:{size:9},maxTicksLimit:6},grid:{color:'#0f172a'},title:{display:true,text:'YES probability',color:'#334155',font:{size:9}}},
              y:{display:true,ticks:{color:'#334155',font:{size:9},maxTicksLimit:4},grid:{color:'#0f172a'}},
            }
          }
        });
      }

      // --- Monte Carlo fan chart ---
      const mcCtx=document.getElementById('ot-mc-chart');
      if(mcCtx&&viz.mc){
        if(window._otCharts.mc){window._otCharts.mc.destroy();}
        const sub2=document.getElementById('ot-mc-subtitle');
        if(sub2&&viz.params){sub2.textContent=`${(viz.params.pnl_win||0).toFixed(2)} win / ${(viz.params.pnl_loss||0).toFixed(2)} loss per trade`;}
        const labels=Array.from({length:viz.mc.n_trades},(_, i)=>i);
        const pathDatasets=(viz.mc.paths||[]).map(path=>({
          data:path,borderColor:'rgba(30,41,59,0.9)',borderWidth:1,pointRadius:0,fill:false,tension:.1,
        }));
        const p10Dataset={data:viz.mc.p10,borderColor:'rgba(239,68,68,.4)',borderWidth:1.5,pointRadius:0,fill:false,tension:.3,borderDash:[3,2]};
        const p90Dataset={data:viz.mc.p90,borderColor:'rgba(16,185,129,.4)',borderWidth:1.5,pointRadius:0,fill:false,tension:.3,borderDash:[3,2]};
        const medianDataset={data:viz.mc.median,borderColor:'#10b981',borderWidth:2.5,pointRadius:0,fill:false,tension:.3};
        window._otCharts.mc=new Chart(mcCtx,{
          type:'line',
          data:{labels,datasets:[...pathDatasets,p10Dataset,p90Dataset,medianDataset]},
          options:{
            responsive:true,maintainAspectRatio:true,animation:false,
            plugins:{legend:{display:false},tooltip:{enabled:false}},
            scales:{
              x:{display:true,ticks:{color:'#334155',font:{size:9},maxTicksLimit:6},grid:{color:'#0f172a'},title:{display:true,text:'Trades',color:'#334155',font:{size:9}}},
              y:{display:true,ticks:{color:'#334155',font:{size:9},maxTicksLimit:4,callback:v=>'$'+v.toFixed(0)},grid:{color:'#0f172a'}},
            }
          }
        });
      }

      // --- Kelly display ---
      const kellyEl=document.getElementById('ot-kelly-display');
      if(kellyEl&&viz.params){
        const p=viz.params;
        const winPct=(p.true_no_prob*100).toFixed(1);
        const lossPct=((1-p.true_no_prob)*100).toFixed(1);
        kellyEl.innerHTML=`
          <div style="color:#06b6d4">f* = (b·p − q) / b</div>
          <div style="color:var(--muted)">b = <span style="color:var(--text)">${p.b.toFixed(4)}</span></div>
          <div style="color:var(--muted)">p = <span style="color:var(--green)">${winPct}%</span> (NO wins)</div>
          <div style="color:var(--muted)">q = <span style="color:var(--red)">${lossPct}%</span> (YES wins)</div>
          <div style="color:var(--muted)">f* = <span style="color:var(--yellow)">${(p.f_star*100).toFixed(2)}%</span></div>
          <div style="color:var(--muted)">×0.25 → <span style="color:#10b981;font-weight:700">${(p.kelly_f*100).toFixed(2)}% Kelly</span></div>
          <div style="color:var(--muted)">Size: <span style="color:var(--text)">$${p.size_usdc.toFixed(0)}</span></div>
        `;
      }

      // --- P&L curve for OT only ---
      const otTrades=ot.recent_trades||[];
      const sellTrades=otTrades.filter(t=>t.side==='SELL').reverse();
      const pnlCtxEl=document.getElementById('ot-pnl-chart');
      if(pnlCtxEl){
        if(window._otCharts.pnl){window._otCharts.pnl.destroy();}
        let running=0;
        const pnlPoints=sellTrades.map(t=>{running+=t.realized_pnl||0;return parseFloat(running.toFixed(2));});
        const pnlLabels=sellTrades.map(t=>new Date(t.timestamp*1000).toLocaleDateString([],{month:'short',day:'numeric'}));
        const totalOtPnl=running;
        const pnlTotalEl=document.getElementById('ot-pnl-total');
        if(pnlTotalEl){pnlTotalEl.textContent=(totalOtPnl>=0?'+':'')+'$'+totalOtPnl.toFixed(2);pnlTotalEl.style.color=totalOtPnl>=0?'var(--green)':'var(--red)';}
        window._otCharts.pnl=new Chart(pnlCtxEl,{
          type:'line',
          data:{
            labels:pnlLabels.length?pnlLabels:['--'],
            datasets:[{
              data:pnlPoints.length?pnlPoints:[0],
              borderColor:'#10b981',borderWidth:2,pointRadius:0,fill:true,
              backgroundColor:'rgba(16,185,129,.07)',tension:.4,
            }]
          },
          options:{
            responsive:true,maintainAspectRatio:true,
            plugins:{legend:{display:false},tooltip:{enabled:false}},
            scales:{
              x:{display:false},
              y:{display:true,ticks:{color:'#334155',font:{size:9},maxTicksLimit:3,callback:v=>'$'+v.toFixed(0)},grid:{color:'#0f172a'}},
            }
          }
        });
      }

      // --- Category breakdown ---
      const catEl=document.getElementById('ot-categories');
      if(catEl){
        const cats=ot.category_counts||{};
        const total=Object.values(cats).reduce((a,b)=>a+b,0)||1;
        const sorted=Object.entries(cats).sort((a,b)=>b[1]-a[1]);
        const catColors={crypto:'#06b6d4',sports:'#f59e0b',weather:'#8b5cf6',politics:'#ec4899',world:'#10b981',entertainment:'#f97316',finance:'#6b7280',unknown:'#475569'};
        catEl.innerHTML=sorted.length?sorted.map(([cat,cnt])=>{
          const pct=Math.round(cnt/total*100);
          const col=catColors[cat]||'#475569';
          return`<div style="margin-bottom:7px">
            <div style="display:flex;justify-content:space-between;margin-bottom:2px">
              <span style="color:${col};font-weight:600">${cat}</span>
              <span style="color:var(--muted)">${cnt}</span>
            </div>
            <div style="background:#0f172a;border-radius:3px;height:4px"><div style="width:${pct}%;height:4px;background:${col};border-radius:3px"></div></div>
          </div>`;
        }).join(''):'<div class="no-data" style="font-size:.68rem">No data</div>';
      }

      // --- Bot status panel ---
      const bsEl=document.getElementById('ot-bot-status');
      if(bsEl){
        const fmt=(v,pfx='')=>v!=null?(pfx+(typeof v==='number'?v.toFixed(2):v)):'--';
        const row=(lbl,val,col='var(--text)')=>`<div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">${lbl}</span><span style="color:${col};font-weight:600">${val}</span></div>`;
        const p=viz.params||{};
        const wr=s.win_rate;
        const wrCol=wr>=95?'var(--green)':wr>=80?'var(--yellow)':'var(--red)';
        bsEl.innerHTML=`
          ${row('Balance',fmt(null),'var(--muted)')}
          ${row('Win Rate',wr!=null?wr.toFixed(1)+'%':'--',wrCol)}
          ${row('W / L',(s.wins||0)+' / '+(s.losses||0))}
          ${row('Avg Edge',((s.avg_edge||0)*100).toFixed(2)+'pp','#06b6d4')}
          ${row('MC P(profit)',s.avg_mc_p_profit!=null?((s.avg_mc_p_profit)*100).toFixed(1)+'%':'--','var(--green)')}
          ${row('YES Range','1¢ – 20¢','var(--yellow)')}
          ${row('Bet Size','$'+fmt(p.size_usdc||0))}
          ${row('Kelly','0.25× Kelly','var(--accent2)')}
          ${row('Strategy','LIMIT ORDER','var(--green)')}
          ${row('Engine','ONLINE','var(--green)')}
        `;
      }

      // --- Recent trades table ---
      const rtEl=document.getElementById('ot-recent-trades');
      if(rtEl){
        const trades=ot.recent_trades||[];
        if(!trades.length){rtEl.innerHTML='<div class="no-data">No trades yet</div>';return;}
        const rows=trades.map(t=>{
          const tStr=new Date(t.timestamp*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
          const pnlTxt=t.side==='SELL'?(t.realized_pnl>0
            ?`<span style="color:var(--green)">+$${t.realized_pnl.toFixed(2)}</span>`
            :`<span style="color:var(--red)">$${t.realized_pnl.toFixed(2)}</span>`):'—';
          const edgeTxt=t.net_edge!=null?((t.net_edge*100).toFixed(2)+'pp'):'—';
          const mcTxt=t.mc_p_profit!=null?((t.mc_p_profit*100).toFixed(0)+'%'):'—';
          const yesTxt=t.yes_ask!=null?(t.yes_ask*100).toFixed(1)+'¢':'—';
          const catColors2={crypto:'#06b6d4',sports:'#f59e0b',weather:'#8b5cf6',politics:'#ec4899',world:'#10b981',entertainment:'#f97316',finance:'#6b7280'};
          const catCol=catColors2[t.category]||'var(--muted)';
          return`<tr style="border-bottom:1px solid #0f172a">
            <td style="color:var(--muted);padding:4px 6px;white-space:nowrap">${tStr}</td>
            <td style="padding:4px 6px;font-weight:700;color:${t.side==='BUY'?'var(--green)':'var(--yellow)'}">${t.side}</td>
            <td style="color:var(--yellow);padding:4px 6px">${yesTxt}</td>
            <td style="padding:4px 6px;color:${catCol};font-weight:600">${t.category||'—'}</td>
            <td style="color:#06b6d4;padding:4px 6px">${edgeTxt}</td>
            <td style="padding:4px 6px">${mcTxt}</td>
            <td style="padding:4px 6px">${pnlTxt}</td>
            <td style="color:var(--muted);padding:4px 6px;max-width:220px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${t.market_question||''}</td>
          </tr>`;
        }).join('');
        rtEl.innerHTML=`<table style="width:100%;border-collapse:collapse">
          <thead><tr style="color:var(--muted);font-size:.6rem;text-transform:uppercase;border-bottom:1px solid var(--border)">
            <th style="padding:4px 6px;text-align:left;font-weight:400">Time</th>
            <th style="padding:4px 6px;text-align:left;font-weight:400">Side</th>
            <th style="padding:4px 6px;text-align:left;font-weight:400">YES</th>
            <th style="padding:4px 6px;text-align:left;font-weight:400">Cat</th>
            <th style="padding:4px 6px;text-align:left;font-weight:400">Edge</th>
            <th style="padding:4px 6px;text-align:left;font-weight:400">MC P</th>
            <th style="padding:4px 6px;text-align:left;font-weight:400">P&amp;L</th>
            <th style="padding:4px 6px;text-align:left;font-weight:400">Market</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
      }
    }).catch(()=>{});
  }

  // Risk health section
  const risk=d.risk||{};
  const score=risk.health_score;
  const grade=risk.health_grade||'N/A';
  const gradeColor=grade==='HEALTHY'?'var(--green)':grade==='WEAK'?'var(--yellow)':grade==='CRITICAL'?'var(--red)':'var(--muted)';
  const drawdown=risk.drawdown_pct||0;
  const exposure=risk.exposure_pct||0;
  const flags=risk.flags||[];
  const scoreDisplay=score!=null?score.toFixed(1):'--';
  const hardStopBadge=risk.hard_stop?`<span style="background:#2a0a0a;color:var(--red);padding:2px 10px;border-radius:6px;font-size:.68rem;font-weight:700;margin-left:10px">HARD STOP</span>`:'';

  const riskEl=$('status-risk');
  if(riskEl) riskEl.innerHTML=`
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">
      <div style="font-size:3rem;font-weight:700;color:${gradeColor};line-height:1">${scoreDisplay}</div>
      <div>
        <div style="font-size:1.1rem;font-weight:700;color:${gradeColor}">${grade}${hardStopBadge}</div>
        <div style="font-size:.68rem;color:var(--muted);margin-top:5px">${flags.length?flags.join(' · '):'No active risk flags'}</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div>
        <div style="font-size:.63rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">Drawdown</div>
        <div style="font-size:.95rem;font-weight:700;color:${drawdown>10?'var(--red)':drawdown>5?'var(--yellow)':'var(--green)'}">${drawdown.toFixed(2)}%</div>
        <div class="health-bar"><div class="health-fill" style="width:${Math.min(drawdown/15*100,100)}%;background:${drawdown>10?'var(--red)':drawdown>5?'var(--yellow)':'var(--green)'}"></div></div>
      </div>
      <div>
        <div style="font-size:.63rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">Exposure</div>
        <div style="font-size:.95rem;font-weight:700;color:${exposure>80?'var(--red)':exposure>60?'var(--yellow)':'var(--green)'}">${exposure.toFixed(1)}%</div>
        <div class="health-bar"><div class="health-fill" style="width:${Math.min(exposure,100)}%;background:${exposure>80?'var(--red)':exposure>60?'var(--yellow)':'var(--green)'}"></div></div>
      </div>
    </div>`;

  // Disk section
  const disk=d.disk||{};
  const diskEl=$('status-disk');
  if(diskEl) diskEl.innerHTML=`
    <div style="display:flex;gap:30px">
      <div><div style="font-size:.63rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em">Log Files</div><div style="font-weight:700;color:var(--accent2);margin-top:5px;font-size:1.1rem">${disk.log_files_count||0}</div></div>
      <div><div style="font-size:.63rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em">Total Size</div><div style="font-weight:700;color:var(--accent2);margin-top:5px;font-size:1.1rem">${(disk.log_files_mb||0).toFixed(2)} MB</div></div>
    </div>`;

  // AI Research integrations panel (System tab)
  const ai=d.ai_research||{};
  const aiOrder=['perplexity','grok','mirofish'];
  const aiHtml=aiOrder.map(key=>{
    const item=ai[key]||{configured:false,label:key,note:'',add_key:'',docs:''};
    const configured=item.configured;
    const dotCls=configured?'ok':'err';
    const statusTxt=configured
      ? '<span style="color:var(--green);font-size:.65rem;font-weight:700;text-transform:uppercase">CONNECTED</span>'
      : `<span style="color:var(--yellow);font-size:.65rem;font-weight:700;text-transform:uppercase">ADD KEY</span>`;
    const keyHint=!configured&&item.add_key
      ? `<div style="font-size:.62rem;color:var(--muted);margin-top:3px;font-family:monospace">${item.add_key}=...</div>`
      : '';
    const docsLink=item.docs
      ? `<a href="${item.docs}" target="_blank" style="font-size:.62rem;color:var(--muted);text-decoration:none;margin-top:4px;display:block">docs ↗</a>`
      : '';
    return`<div class="status-item" style="align-items:flex-start;padding:10px 0">
      <div class="dot ${dotCls}" style="margin-top:3px"></div>
      <div style="flex:1">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="status-label" style="font-size:.8rem">${item.label}</div>
          ${statusTxt}
        </div>
        <div style="font-size:.66rem;color:var(--muted);margin-top:3px">${item.note||''}</div>
        ${keyHint}${docsLink}
      </div>
    </div>`;
  }).join('');
  const aiEl=$('status-ai-research');
  if(aiEl) aiEl.innerHTML=aiHtml||'<div class="no-data">No AI integrations configured</div>';

  // Also update AI Intel tab status cards if on that tab
  const aiIntelEl=$('ai-intel-status-cards');
  if(aiIntelEl){
    const intelHtml=aiOrder.map(key=>{
      const item=ai[key]||{configured:false,label:key,note:'',add_key:'',docs:''};
      const configured=item.configured;
      return`<div class="ai-status-card ${configured?'configured':'missing'}">
        <div class="ai-card-name">${item.label}</div>
        <div class="ai-card-status" style="color:${configured?'var(--green)':'var(--muted)'}">${configured?'Connected':'Not configured'}</div>
        <div class="ai-card-note">${item.note||''}</div>
        ${!configured&&item.add_key?`<div class="ai-card-key">${item.add_key}=...</div>`:''}
        ${item.docs?`<a href="${item.docs}" target="_blank" style="font-size:.63rem;color:var(--muted);text-decoration:none;margin-top:6px;display:block">docs ↗</a>`:''}
      </div>`;
    }).join('');
    aiIntelEl.innerHTML=intelHtml;
  }
}

// ------------------------------------------------------------------ //
//  Kalshi diagnostics                                                  //
// ------------------------------------------------------------------ //
function renderKalshiDiag(k){
  const el=$('kalshi-diag');
  if(!el)return;
  const overall=k.overall_status==='active';
  const statusColor=overall?'var(--green)':'var(--yellow)';
  const statusText=overall?'ACTIVE':'SETUP REQUIRED';

  // Checklist
  const checkHtml=(k.checklist||[]).map(c=>`
    <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:1rem;margin-top:1px">${c.ok?'✅':'❌'}</span>
      <div style="flex:1">
        <div style="font-size:.75rem;font-weight:700;color:${c.ok?'var(--green)':'var(--yellow)'}">${c.label}</div>
        ${!c.ok?`<div style="font-size:.66rem;color:var(--muted);margin-top:3px;font-family:monospace">${c.fix}</div>`:''}
      </div>
    </div>`).join('');

  // Runtime info
  const rt=k.runtime||{};
  const runtimeHtml=rt.client_loaded?`
    <div style="display:flex;gap:20px;margin-top:10px;font-size:.72rem;flex-wrap:wrap">
      <div><span style="color:var(--muted)">Markets cached:</span> <span style="color:var(--accent2);font-weight:700">${rt.markets_cached||0}</span></div>
      <div><span style="color:var(--muted)">Cache age:</span> <span style="color:var(--accent2);font-weight:700">${rt.last_cache_age_s!=null?rt.last_cache_age_s+'s':'--'}</span></div>
      <div><span style="color:var(--muted)">Auth:</span> <span style="color:var(--accent2);font-weight:700">${k.auth_method||'--'}</span></div>
      <div><span style="color:var(--muted)">Min edge:</span> <span style="color:var(--accent2);font-weight:700">${k.min_edge_pct||5}%</span></div>
      <div><span style="color:var(--muted)">Safe-only:</span> <span style="color:var(--accent2);font-weight:700">${k.safe_only?'Yes (crypto price only)':'No'}</span></div>
    </div>`:'<div style="font-size:.7rem;color:var(--muted);margin-top:8px">Client not loaded — enable KALSHI_ENABLED=true and redeploy.</div>';

  // Recent decisions
  const decisions=k.recent_decisions||[];
  const decHtml=decisions.length?`
    <div style="margin-top:12px">
      <div style="font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:6px">Recent Cross-Exchange Scans (last 20)</div>
      <div style="display:flex;flex-direction:column;gap:4px;max-height:260px;overflow-y:auto">
        ${decisions.map(d=>{
          const age=Math.round((Date.now()/1000)-(d.ts||0));
          const edgePct=d.edge!=null?(d.edge*100).toFixed(2)+'%':'--';
          const color=d.signal?'var(--green)':d.skipped?'var(--yellow)':'var(--muted)';
          const label=d.signal?'SIGNAL':d.skipped?'SKIPPED':'SCANNED';
          return`<div style="display:flex;gap:8px;align-items:center;padding:5px 8px;background:var(--bg);border-radius:6px;border-left:3px solid ${color};font-size:.68rem">
            <span style="font-weight:700;color:${color};min-width:58px">${label}</span>
            <span style="color:var(--muted);min-width:36px">${age}s ago</span>
            <span style="color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.poly_question||d.ticker||''}</span>
            <span style="color:var(--yellow);min-width:40px;text-align:right">${edgePct}</span>
          </div>`;
        }).join('')}
      </div>
    </div>`:`<div style="font-size:.7rem;color:var(--muted);margin-top:10px">${overall?'No cross-exchange scans logged yet — strategy running.':'Enable Kalshi to see scan decisions here.'}</div>`;

  el.innerHTML=`
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <span style="font-size:1.1rem;font-weight:700;color:${statusColor}">${statusText}</span>
      <span style="font-size:.66rem;color:var(--muted)">${k.auth_method||'No credentials set'}</span>
    </div>
    ${checkHtml}
    ${runtimeHtml}
    ${decHtml}`;
  updateQrBadge(decisions);
}

// ------------------------------------------------------------------ //
//  Analytics tab                                                       //
// ------------------------------------------------------------------ //
async function fetchAnalytics(){
  try{
    const [d,status]=await Promise.all([
      fetch('/api/analytics').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
    ]);
    renderAnalytics(d);
    renderTimeToProfit(status,d);
  }catch(e){
    console.error('Analytics fetch failed',e);
  }
}

function fmtDuration(hours){
  if(hours==null||!isFinite(hours))return'--';
  if(hours<1)return Math.round(hours*60)+'m';
  if(hours<24)return hours.toFixed(1)+'h';
  const d=Math.floor(hours/24),h=Math.round(hours%24);
  return h>0?d+'d '+h+'h':d+'d';
}

function renderTimeToProfit(status,analytics){
  const closed=status.closed_positions||0;
  const uptimeH=(status.uptime_seconds||0)/3600;
  const realizedPnl=status.realized_pnl||0;
  const startBal=status.starting_balance||10000;

  // Close rate: positions closed per hour (needs >10min uptime to be meaningful)
  const closesPerHr=uptimeH>0.17?closed/uptimeH:null;

  // Hourly realized-PnL rate from last 6 data points in pnl_history (use analytics.hourly_pnl)
  const hourly=analytics.hourly_pnl||[];
  let hourlyRate=null;
  if(hourly.length>=2){
    const recent=hourly.slice(-6);
    const total=recent.reduce((s,h)=>s+h.pnl,0);
    hourlyRate=total/recent.length;
  }

  // Milestone definitions
  const BOOTSTRAP=30;   // need 30 closed positions to exit bootstrap
  const RELIABLE=50;    // 50 closed → win rate is statistically meaningful
  const CONFIDENT=100;  // 100 closed → strategy ROI is trustworthy

  const milestones=[
    {
      id:'bootstrap',
      label:'Exit Bootstrap Phase',
      sub:'30 closed positions → P&L data is meaningful',
      target:BOOTSTRAP,
      current:closed,
      hoursLeft:closed<BOOTSTRAP&&closesPerHr>0?(BOOTSTRAP-closed)/closesPerHr:null,
      done:closed>=BOOTSTRAP,
      color:'#ffd740',
    },
    {
      id:'reliable',
      label:'Reliable Win Rate',
      sub:'50 closed positions → win% stabilizes',
      target:RELIABLE,
      current:closed,
      hoursLeft:closed<RELIABLE&&closesPerHr>0?(RELIABLE-closed)/closesPerHr:null,
      done:closed>=RELIABLE,
      color:'#00e5ff',
    },
    {
      id:'confident',
      label:'Strategy Confidence',
      sub:'100 closed positions → trust per-strategy ROI',
      target:CONFIDENT,
      current:closed,
      hoursLeft:closed<CONFIDENT&&closesPerHr>0?(CONFIDENT-closed)/closesPerHr:null,
      done:closed>=CONFIDENT,
      color:'#7986cb',
    },
    {
      id:'breakeven',
      label:'Break-Even Realized P&L',
      sub:'Realized P&L turns positive',
      target:null,
      current:null,
      hoursLeft:realizedPnl<0&&hourlyRate>0?Math.abs(realizedPnl)/hourlyRate:null,
      done:realizedPnl>=0,
      color:'#00e676',
      isBreakeven:true,
    },
  ];

  // Phase badge
  let phase,phaseClass;
  if(closed>=CONFIDENT){phase='Strategy Proven';phaseClass='profit';}
  else if(closed>=RELIABLE){phase='Reliable Data';phaseClass='active';}
  else if(closed>=BOOTSTRAP){phase='Active Trading';phaseClass='active';}
  else{phase='Bootstrap Phase';phaseClass='bootstrap';}
  const badge=$('ttpl-phase-badge');
  badge.textContent=phase;
  badge.className='ttpl-badge '+phaseClass;

  // Render milestone cards
  $('ttpl-milestones').innerHTML=milestones.map(m=>{
    if(m.done){
      const label=m.isBreakeven
        ?'<span style="color:#00e676">+$'+realizedPnl.toFixed(2)+' realized</span>'
        :'<span style="color:#00e676">✓ Done ('+closed+' closed)</span>';
      return`<div class="ttpl-milestone done">
        <div class="ttpl-ms-label">${m.label}</div>
        <div class="ttpl-ms-eta">${label}</div>
        <div class="ttpl-ms-sub">${m.sub}</div>
        <div class="ttpl-ms-bar"><div class="ttpl-ms-fill" style="width:100%;background:${m.color}"></div></div>
      </div>`;
    }

    let etaText,pct,subLine;
    if(m.isBreakeven){
      if(realizedPnl>=0){
        etaText='In profit';pct=100;
      }else if(hourlyRate===null){
        etaText='Needs data';pct=0;
      }else if(hourlyRate<=0){
        etaText='Trending negative';pct=0;
      }else{
        etaText='~'+fmtDuration(m.hoursLeft);
        // progress toward $0 from starting low watermark
        const worst=Math.min(realizedPnl,-0.01);
        pct=Math.max(0,Math.min(99,(1-Math.abs(realizedPnl)/Math.abs(worst))*100));
      }
      subLine=hourlyRate!=null&&hourlyRate>0?'At $'+hourlyRate.toFixed(2)+'/hr current rate':m.sub;
    }else{
      if(closesPerHr===null){etaText='Needs data';pct=0;}
      else{
        etaText='~'+fmtDuration(m.hoursLeft);
        pct=Math.min(99,Math.max(0,(m.current/m.target)*100));
      }
      subLine=m.current+' / '+m.target+' closed'+(closesPerHr?` · ${closesPerHr.toFixed(1)}/hr`:'');
    }

    return`<div class="ttpl-milestone">
      <div class="ttpl-ms-label">${m.label}</div>
      <div class="ttpl-ms-eta">${etaText}</div>
      <div class="ttpl-ms-sub">${subLine}</div>
      <div class="ttpl-ms-bar"><div class="ttpl-ms-fill" style="width:${pct}%;background:${m.color}"></div></div>
    </div>`;
  }).join('');

  // Verdict text
  let verdict='';
  if(closed<BOOTSTRAP){
    const h=closesPerHr>0?fmtDuration((BOOTSTRAP-closed)/closesPerHr):'unknown time';
    verdict=`You're in the <strong style="color:#ffd740">bootstrap phase</strong> (${closed}/${BOOTSTRAP} closed positions). `
      +`P&L numbers exist but aren't statistically reliable yet. Estimated <strong>${h}</strong> until the data is meaningful. `
      +`The meta-agent won't auto-tune parameters until bootstrap exits.`;
  }else if(closed<RELIABLE){
    verdict=`Bootstrap cleared! Win rate and ROI numbers are forming but need ${RELIABLE-closed} more closed positions to stabilize. `
      +`Watch the Strategy ROI chart — strategies with negative ROI after ${RELIABLE} trades are candidates to disable.`;
  }else if(realizedPnl<0){
    const etaStr=hourlyRate>0?'~'+fmtDuration(Math.abs(realizedPnl)/hourlyRate)+' at current pace':'unclear (needs more recent trade data)';
    verdict=`Data is reliable (${closed} closed positions). Realized P&L is currently <strong style="color:#ff5252">$${realizedPnl.toFixed(2)}</strong>. `
      +`Break-even estimated in <strong>${etaStr}</strong>. Focus on strategies with positive ROI and disable outliers.`;
  }else{
    verdict=`<strong style="color:#00e676">You're in profit.</strong> Realized P&L: <strong>+$${realizedPnl.toFixed(2)}</strong> across ${closed} closed positions. `
      +`Win rate and strategy ROI data below reflect real performance.`;
  }
  $('ttpl-verdict').innerHTML=verdict;
}

function hbar(labels,values,colors){
  return{
    type:'bar',
    data:{
      labels,
      datasets:[{data:values,backgroundColor:colors,borderColor:'transparent',borderWidth:0}]
    },
    options:{
      indexAxis:'y',
      responsive:true,
      maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{
        x:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}},
        y:{grid:{color:'#1a1a1a'},ticks:{color:'#aaa',font:{size:10}}}
      }
    }
  };
}

function renderAnalytics(d){
  // Strategy ROI chart
  const roiEntries=Object.entries(d.strategy_roi||{});
  if(roiEntries.length){
    const labels=roiEntries.map(([k])=>k);
    const values=roiEntries.map(([,v])=>v);
    const colors=values.map(v=>v>=0?'rgba(0,230,118,.6)':'rgba(255,82,82,.6)');
    if(roiChart){roiChart.destroy();}
    roiChart=new Chart($('roiChart').getContext('2d'),hbar(labels,values,colors));
  }

  // Win rate chart
  const wrEntries=Object.entries(d.strategy_win_rates||{});
  if(wrEntries.length){
    const labels=wrEntries.map(([k])=>k);
    const values=wrEntries.map(([,v])=>v);
    const colors=values.map(v=>v>=50?'rgba(0,229,255,.6)':'rgba(255,215,64,.6)');
    if(winRateChart){winRateChart.destroy();}
    winRateChart=new Chart($('winRateChart').getContext('2d'),hbar(labels,values,colors));
  }

  // Hourly PnL chart
  const hourly=d.hourly_pnl||[];
  if(hourly.length){
    if(hourlyPnlChart){hourlyPnlChart.destroy();}
    hourlyPnlChart=new Chart($('hourlyPnlChart').getContext('2d'),{
      type:'bar',
      data:{
        labels:hourly.map(h=>h.hour_label),
        datasets:[{
          data:hourly.map(h=>h.pnl),
          backgroundColor:hourly.map(h=>h.pnl>=0?'rgba(0,230,118,.6)':'rgba(255,82,82,.6)'),
          borderWidth:0
        }]
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{x:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}},y:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}}}
      }
    });
  }

  // Fee drag chart
  const feeEntries=Object.entries(d.strategy_fees||{});
  if(feeEntries.length){
    const labels=feeEntries.map(([k])=>k);
    const values=feeEntries.map(([,v])=>v);
    if(feeDragChart){feeDragChart.destroy();}
    feeDragChart=new Chart($('feeDragChart').getContext('2d'),hbar(labels,values,values.map(()=>'rgba(255,112,67,.6)')));
  }

  // Health trend chart
  const hh=d.health_history||[];
  if(hh.length){
    if(healthTrendChart){healthTrendChart.destroy();}
    healthTrendChart=new Chart($('healthTrendChart').getContext('2d'),{
      type:'line',
      data:{
        labels:hh.map(h=>new Date(h.t*1000).toLocaleString()),
        datasets:[{
          label:'Health Score',
          data:hh.map(h=>h.score),
          borderColor:'#7986cb',
          backgroundColor:'rgba(121,134,203,.1)',
          borderWidth:1.5,
          pointRadius:3,
          fill:true,
          tension:.3
        }]
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{
          x:{display:false,grid:{color:'#1a1a1a'}},
          y:{min:0,max:100,grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}}
        }
      }
    });
  }else{
    $('healthTrendChart').closest('.section').querySelector('h3').insertAdjacentHTML('afterend','<div class="no-data" style="padding:20px">No health history yet — run meta-agent first</div>');
  }

  // LLM decisions table
  const llm=d.llm_decisions||[];
  const now=Date.now()/1000;
  if(llm.length){
    $('llm-decisions-table').innerHTML=`<table>
      <tr><th>Time</th><th>Strategy</th><th>Side</th><th>Price</th><th>Amount</th><th>Notes</th></tr>
      ${llm.map(t=>`<tr>
        <td class="ts-small">${ts(t.timestamp)}</td>
        <td>${badge(t.strategy)}</td>
        <td class="${t.side.toLowerCase()}">${t.side}</td>
        <td>${fmtN(t.price)}</td>
        <td>${fmt(t.usdc_amount)}</td>
        <td style="color:#7986cb;font-size:.68rem">${t.notes}</td>
      </tr>`).join('')}
    </table>`;
  }

  // Active signals: LLM trades in last hour
  const active=llm.filter(t=>now-t.timestamp<3600);
  if(active.length){
    $('llm-active-signals').innerHTML=`<div style="display:flex;flex-wrap:wrap;gap:8px">${active.map(t=>`
      <div style="background:#1a1a2a;border:1px solid #7986cb;border-radius:6px;padding:8px 12px;font-size:.72rem">
        <span class="${t.side.toLowerCase()}">${t.side}</span> via ${badge(t.strategy)} · ${fmtN(t.price)} · ${fmt(t.usdc_amount)}
        <div style="color:#555;margin-top:3px">${t.notes.substring(0,80)}</div>
      </div>`).join('')}</div>`;
  }else{
    $('llm-active-signals').innerHTML='<div class="no-data">None in last hour</div>';
  }

  // Parameter change timeline from meta history
  fetch('/api/meta/history').then(r=>r.json()).then(hist=>{
    const changes=[];
    hist.forEach(h=>{
      const applied=h.applied_changes||[];
      const proposed=h.proposed_changes||{};
      if(applied.length){
        changes.push({
          t:h.timestamp,
          keys:applied,
          proposed
        });
      }
    });
    if(changes.length){
      $('param-timeline').innerHTML=`<div style="display:flex;flex-direction:column;gap:8px">${changes.map(c=>`
        <div style="display:flex;gap:12px;align-items:flex-start">
          <div class="ts-small" style="white-space:nowrap;min-width:120px;margin-top:2px">${tsDate(c.t)}</div>
          <div>${c.keys.map(k=>`<span style="background:#1a2a1a;color:#00e676;padding:1px 7px;border-radius:3px;font-size:.65rem;margin-right:4px">${k} → ${c.proposed[k]||'?'}</span>`).join('')}</div>
        </div>`).join('')}
      </div>`;
    }else{
      $('param-timeline').innerHTML='<div class="no-data">No parameter changes applied yet</div>';
    }
  }).catch(()=>{});
}

// ------------------------------------------------------------------ //
//  Balances tab                                                        //
// ------------------------------------------------------------------ //
async function fetchBalances(){
  try{
    const d=await fetch('/api/balances').then(r=>r.json());
    renderBalances(d);
  }catch(e){
    console.error('Balances fetch failed',e);
  }
}

function fmtUsd(n){
  if(n==null||n===undefined)return'--';
  return'$'+Number(n).toFixed(2);
}

function renderBalances(d){
  const cy=d.billing_cycle||{};
  const ant=d.anthropic||{};
  const rail=d.railway||{};
  const bot=d.bot||{};

  // Billing cycle bar
  const pct=cy.cycle_pct||0;
  $('cycle-fill').style.width=pct+'%';
  $('cycle-pct').textContent=pct.toFixed(1);
  $('cycle-start').textContent=cy.start||'--';
  $('cycle-end').textContent=cy.end||'--';
  $('cycle-days-left').textContent=(cy.days_remaining||0)+' days remaining';

  // Anthropic
  $('bal-ant-model').textContent=ant.model||'--';
  $('bal-ant-key').innerHTML=ant.key_configured
    ?'<span style="color:#00e676">Configured</span>'
    :'<span style="color:#ff5252">Not set</span>';
  $('bal-ant-runs').textContent=ant.meta_agent_runs||0;
  $('bal-ant-cpr').textContent=fmtUsd(ant.cost_per_run_usd);
  $('bal-ant-cost').textContent=fmtUsd(ant.estimated_cost_usd);
  $('bal-ant-proj').textContent=fmtUsd(ant.projected_monthly_usd);
  if(ant.monthly_budget_usd){
    $('bal-ant-budget').textContent=fmtUsd(ant.monthly_budget_usd);
    const usedPct=Math.min((ant.estimated_cost_usd/ant.monthly_budget_usd)*100,100);
    $('bal-ant-bar').style.width=usedPct+'%';
    $('bal-ant-bar').style.background=usedPct>80?'#ff5252':usedPct>60?'#ffd740':'#ce93d8';
    $('bal-ant-bar-lbl').textContent=fmtUsd(ant.estimated_cost_usd)+' used of '+fmtUsd(ant.monthly_budget_usd);
    $('bal-ant-bar-pct').textContent=usedPct.toFixed(1)+'%';
  }else{
    $('bal-ant-bar').style.width='0%';
    const projPct=ant.projected_monthly_usd||0;
    $('bal-ant-bar-lbl').textContent='Projected this month: '+fmtUsd(projPct);
  }

  // Railway
  $('bal-rail-base').textContent=fmtUsd(rail.plan_base_cost_usd)+'/mo';
  $('bal-rail-days').textContent=(rail.days_remaining_in_cycle||0)+' days';
  const diskMb=rail.disk_used_mb||0;
  const diskLim=rail.disk_limit_mb||512;
  $('bal-rail-disk').textContent=diskMb.toFixed(2)+' MB / '+diskLim+' MB';
  const diskPct=rail.disk_pct||0;
  $('bal-rail-bar').style.width=Math.min(diskPct,100)+'%';
  $('bal-rail-bar').style.background=diskPct>80?'#ff5252':diskPct>60?'#ffd740':'#4dd0e1';
  $('bal-rail-bar-pct').textContent=diskPct.toFixed(1)+'%';

  // Bot
  $('bal-bot-mode').innerHTML=bot.paper_trading
    ?'<span style="color:#00e5ff">PAPER</span>'
    :'<span style="color:#ff5252">LIVE</span>';
  const uh=bot.uptime_hours||0;
  const uptimeStr=uh>=24?Math.floor(uh/24)+'d '+Math.round(uh%24)+'h':uh.toFixed(1)+'h';
  $('bal-bot-uptime').textContent=uptimeStr;
  $('bal-bot-trades').textContent=bot.trades_executed||0;
  const pnl=bot.total_pnl_usd||0;
  $('bal-bot-pnl').innerHTML='<span style="color:'+(pnl>=0?'#00e676':'#ff5252')+'">'+fmtUsd(pnl)+'</span>';
  // Cycles left: 30-min meta-agent cadence × days remaining × 48 runs/day
  const metaRunsPerDay=48;
  const botDaysLeft=cy.days_remaining||0;
  $('bal-bot-cycles').textContent=Math.round(botDaysLeft*24*2)+' scan cycles';
  $('bal-bot-metaruns').textContent=Math.round(botDaysLeft*metaRunsPerDay)+' runs';
}

// ------------------------------------------------------------------ //
//  Research tab                                                        //
// ------------------------------------------------------------------ //
async function fetchResearch(){
  try{
    const [latest,history,signals,proposals]=await Promise.all([
      fetch('/api/research/latest').then(r=>r.json()),
      fetch('/api/research/list').then(r=>r.json()),
      fetch('/api/research/signals').then(r=>r.json()),
      fetch('/api/research/proposals/list').then(r=>r.json()),
    ]);
    renderResearch(latest,history);
    renderResearchSignals(signals);
    renderProposals(proposals);
  }catch(e){
    console.error('Research fetch failed',e);
  }
}

function renderResearch(d,history){
  const intervalH=parseFloat(localStorage.getItem('researchInterval')||'2');

  if(!d||!d.found){
    $('res-last').textContent='Never';
    $('res-next').textContent='soon';
    $('res-total').textContent='--';
    $('res-high').textContent='--';
    $('res-websearch').textContent='--';
    $('res-interval').textContent='every '+intervalH+'h';
    return;
  }

  // Header cards
  $('res-last').textContent=(d.date||'')+(d.run_hour?' '+d.run_hour:'');
  const nextTs=(d.timestamp||0)+intervalH*3600;
  const diffM=Math.round((nextTs-Date.now()/1000)/60);
  $('res-next').textContent=diffM>0?'in ~'+diffM+'m':'soon';
  $('res-total').textContent=(d.finding_count||0)+' total';
  $('res-high').innerHTML='<span style="color:#00e676">'+(d.high_count||0)+' high</span> · <span style="color:#ffd740">'+(d.medium_count||0)+' medium</span>';
  const ws=d.web_search_used;
  $('res-websearch').innerHTML=ws
    ?'<span style="color:#00e676">Live</span>'
    :'<span style="color:#ffd740">Training data</span>';
  $('res-interval').textContent='every '+intervalH+'h';
  $('res-run-label').textContent='Run #'+(d.run_index!=null?d.run_index:'?')+' · '+((d.topics_searched||[]).length)+' topics searched';

  // Top insights
  const insights=d.top_insights||[];
  if(insights.length){
    $('res-insights').innerHTML=insights.map(ins=>`
      <div class="res-insight">
        <span class="res-insight-bullet">◆</span>
        <span>${ins}</span>
      </div>`).join('');
  }else{
    $('res-insights').innerHTML='<div class="no-data">No top insights extracted.</div>';
  }

  // Topics label
  const topics=d.topics_searched||[];
  if(topics.length){
    $('res-topics-label').textContent='— searched: '+topics.join(' · ');
  }

  // Findings
  const findings=d.findings||[];
  const relOrder={high:0,medium:1,low:2};
  const sorted=[...findings].sort((a,b)=>(relOrder[a.relevance]||9)-(relOrder[b.relevance]||9));
  if(sorted.length){
    $('res-findings').innerHTML=sorted.map(f=>`
      <div class="res-finding ${f.relevance||'low'}">
        <div class="res-finding-meta">
          <span class="res-rel ${f.relevance||'low'}">${f.relevance||'low'}</span>
          <span class="res-cat">${f.category||''}</span>
          <span class="res-source">${f.source||''}</span>
        </div>
        <div class="res-title">${f.title||''}</div>
        <div class="res-summary">${f.summary||''}</div>
        ${f.actionable_suggestion?`<div class="res-suggestion">→ ${f.actionable_suggestion}</div>`:''}
      </div>`).join('');
  }else{
    $('res-findings').innerHTML='<div class="no-data">No findings in this run.</div>';
  }

  // Suggested experiments
  const exps=d.suggested_experiments||[];
  if(exps.length){
    $('res-experiments').innerHTML=exps.map((e,i)=>`
      <div class="res-experiment">
        <span class="res-exp-num">${i+1}.</span>
        <span>${e}</span>
      </div>`).join('');
  }else{
    $('res-experiments').innerHTML='<div class="no-data">No experiments suggested.</div>';
  }

  // History table
  if(history&&history.length){
    $('res-history').innerHTML=`<table>
      <tr><th>Date</th><th>Time</th><th>Findings</th><th>Web</th><th>Topics</th><th>Top Insight</th></tr>
      ${history.map(r=>`<tr>
        <td class="ts-small">${r.date||'--'}</td>
        <td class="ts-small">${r.run_hour||'--'}</td>
        <td><span style="color:#00e676">${r.high_count||0}H</span> / ${r.finding_count||0} total</td>
        <td>${r.web_search_used?'<span style="color:#00e676">✓</span>':'<span style="color:#555">✗</span>'}</td>
        <td style="color:#555;font-size:.65rem">${(r.topics_searched||[]).slice(0,2).map(t=>t.split(' ').slice(0,3).join(' ')).join(', ')}</td>
        <td style="color:#888;font-size:.68rem">${(r.top_insights||[])[0]||'--'}</td>
      </tr>`).join('')}
    </table>`;
  }
}

// ------------------------------------------------------------------ //
//  Code Review tab                                                     //
// ------------------------------------------------------------------ //
async function fetchCodeReview(){
  try{
    const [latest,history]=await Promise.all([
      fetch('/api/code_review/latest').then(r=>r.json()),
      fetch('/api/code_review/list').then(r=>r.json()),
    ]);
    renderCodeReview(latest,history);
  }catch(e){
    console.error('Code review fetch failed',e);
  }
}

function renderCodeReview(d,history){
  if(!d||!d.found){
    $('cr-grade').textContent='--';
    $('cr-score').textContent='--';
    $('cr-date').textContent='Never';
    $('cr-total').textContent='--';
    $('cr-severity').textContent='runs weekly';
    return;
  }

  const grade=d.grade||'?';
  const gradeEl=$('cr-grade');
  gradeEl.textContent=grade;
  gradeEl.className='val cr-grade-'+grade;

  const score=d.health_score;
  const scoreEl=$('cr-score');
  scoreEl.textContent=score!=null?score:'--';
  scoreEl.className='val '+(score>=75?'green':score>=50?'yellow':'red');

  $('cr-date').textContent=d.date||'--';
  $('cr-total').textContent=(d.total_findings||0)+' total';
  $('cr-severity').textContent=(d.high_findings||0)+' high · '+(d.medium_findings||0)+' medium · '+(d.low_findings||0)+' low';

  // Summary
  $('cr-summary').textContent=d.summary||'No summary.';

  // Strengths
  const strengths=d.strengths||[];
  if(strengths.length){
    $('cr-strengths-card').style.display='';
    $('cr-strengths').innerHTML=strengths.map(s=>`<div class="cr-strength"><span style="color:#00e676">✓</span>${s}</div>`).join('');
  }

  // Findings
  const findings=d.findings||[];
  if(!findings.length){
    $('cr-findings').innerHTML='<div class="no-data">No findings — code looks clean!</div>';
  }else{
    const sevOrder={high:0,medium:1,low:2,info:3};
    const sorted=[...findings].sort((a,b)=>(sevOrder[a.severity]||9)-(sevOrder[b.severity]||9));
    $('cr-findings').innerHTML=sorted.map(f=>`
      <div class="cr-finding ${f.severity||'info'}">
        <div class="cr-finding-header">
          <span class="cr-sev ${f.severity||'info'}">${f.severity||'info'}</span>
          <span class="cr-cat">${f.category||''}</span>
          <span class="cr-file">${f.file||''}</span>
        </div>
        <div class="cr-title">${f.title||''}</div>
        <div class="cr-desc">${f.description||''}</div>
        ${f.suggestion?`<div class="cr-suggestion">💡 ${f.suggestion}</div>`:''}
      </div>`).join('');
  }

  // History table
  if(history&&history.length){
    $('cr-history').innerHTML=`<table>
      <tr><th>Date</th><th>Grade</th><th>Score</th><th>Findings</th><th>Summary</th></tr>
      ${history.map(r=>`<tr>
        <td class="ts-small">${r.date||'--'}</td>
        <td class="cr-grade-${r.grade||'?'}" style="font-weight:700">${r.grade||'?'}</td>
        <td>${r.health_score!=null?r.health_score:'--'}</td>
        <td>${r.high_findings||0}H / ${r.medium_findings||0}M / ${(r.total_findings||0)-(r.high_findings||0)-(r.medium_findings||0)}L</td>
        <td style="color:#555;font-size:.7rem">${r.summary||''}</td>
      </tr>`).join('')}
    </table>`;
  }
}

async function fetchMeta(){
  const [hist,latest]=await Promise.all([
    fetch('/api/meta/history').then(r=>r.json()),
    fetch('/api/meta/latest').then(r=>r.json()),
  ]);
  $('meta-count').textContent=hist.length;
  $('meta-last').textContent=hist.length?tsDate(hist[0].timestamp):'Never';
  if(hist.length){
    const nextTs=(hist[0].timestamp||0)+1800;
    const diff=Math.round((nextTs-Date.now()/1000)/60);
    $('meta-next').textContent=diff>0?'in ~'+diff+'m':'soon';
  }

  if(latest.found){
    const ch=latest.proposed_changes||{};
    const rows=Object.entries(ch).map(([k,v])=>`<tr><td>${k}</td><td>${latest.current_values?.[k]||'?'}</td><td>${v}</td></tr>`).join('');
    $('meta-latest-card').innerHTML=`
      <div class="meta-card">
        <h3>Latest Analysis — ${tsDate(latest.timestamp)}</h3>
        <div class="meta-analysis">${latest.analysis||''}</div>
        ${rows?`<br><table class="change-table"><tr><th>Parameter</th><th>Was</th><th>Proposed</th></tr>${rows}</table>`:'<p style="color:#555;margin-top:8px;font-size:.75rem">No parameter changes suggested.</p>'}
      </div>`;
  }

  if(hist.length){
    $('meta-history').innerHTML=`<table>
      <tr><th>Time</th><th>Portfolio P&L</th><th>Changes Suggested</th><th>Preview</th></tr>
      ${hist.map(h=>`<tr>
        <td class="ts-small">${tsDate(h.timestamp)}</td>
        <td class="${h.portfolio_pnl>=0?'buy':'sell'}">${fmtPnl(h.portfolio_pnl)}</td>
        <td>${Object.keys(h.proposed_changes||{}).length} suggested / <span class="win">${(h.applied_changes||[]).length} applied</span></td>
        <td style="color:#555">${h.analysis_preview}</td>
      </tr>`).join('')}
    </table>`;
  }else{
    $('meta-history').innerHTML='<div class="no-data">No analyses yet.</div>';
  }
}

const evtSource=new EventSource('/api/logs/stream');
evtSource.onmessage=e=>{
  const entry=JSON.parse(e.data);
  const feed=$('log-feed');
  const d=document.createElement('div');
  d.className='log-line';
  const t=new Date(entry.t*1000).toLocaleTimeString('en',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:2});
  d.innerHTML=`<span class="ts">${t}</span><span class="lvl ${entry.level}">${entry.level.substring(0,4)}</span>${entry.msg}`;
  feed.appendChild(d);
  if($('autoscroll').checked)feed.scrollTop=feed.scrollHeight;
  while(feed.children.length>500)feed.removeChild(feed.firstChild);
};

fetch('/api/logs?limit=200').then(r=>r.json()).then(logs=>{
  const feed=$('log-feed');
  logs.forEach(entry=>{
    const d=document.createElement('div');
    d.className='log-line';
    const t=new Date(entry.t*1000).toLocaleTimeString('en',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
    d.innerHTML=`<span class="ts">${t}</span><span class="lvl ${entry.level}">${entry.level.substring(0,4)}</span>${entry.msg}`;
    feed.appendChild(d);
  });
  feed.scrollTop=feed.scrollHeight;
});

async function resetPortfolio(){
  if(!confirm('Reset portfolio to $10,000? This will erase all trades, positions, and history.'))return;
  try{
    const r=await fetch('/api/reset',{method:'POST',headers:{'X-Api-Key':window._dashApiKey||''}});
    const d=await r.json();
    if(d.ok){
      alert('Portfolio reset to $'+d.starting_balance.toLocaleString());
      fetchAll();
    }else{
      alert('Reset failed: '+(d.error||'unknown error'));
    }
  }catch(e){alert('Reset failed: '+e);}
}

async function addFunds(){
  const input=prompt('Add virtual USDC (default $10,000):','10000');
  if(input===null)return;
  const amount=parseFloat(input.replace(/[,$]/g,''));
  if(isNaN(amount)||amount<=0){alert('Invalid amount');return;}
  try{
    const r=await fetch(`/api/add-funds?amount=${amount}`,{method:'POST',headers:{'X-Api-Key':window._dashApiKey||''}});
    const d=await r.json();
    if(d.ok){
      alert(`Added $${amount.toLocaleString()} — new balance: $${d.usdc_balance.toLocaleString()}`);
      fetchAll();
    }else{
      alert('Failed: '+(d.error||'unknown error'));
    }
  }catch(e){alert('Failed: '+e);}
}

// ------------------------------------------------------------------ //
//  Auto-fix                                                           //
// ------------------------------------------------------------------ //
let _autofixPolling=null;

async function triggerAutofix(){
  const btn=$('cr-autofix-btn');
  btn.disabled=true;
  btn.textContent='Running…';
  $('cr-autofix-status').textContent='Starting…';
  $('cr-autofix-results').innerHTML='';
  try{
    const r=await fetch('/api/code_review/autofix',{method:'POST'});
    const d=await r.json();
    if(!d.ok){
      $('cr-autofix-status').textContent='Error: '+(d.error||'unknown');
      btn.disabled=false;btn.textContent='▶ Run Auto-Fix';
      return;
    }
  }catch(e){
    $('cr-autofix-status').textContent='Request failed: '+e;
    btn.disabled=false;btn.textContent='▶ Run Auto-Fix';
    return;
  }
  // Start polling
  if(_autofixPolling)clearInterval(_autofixPolling);
  _autofixPolling=setInterval(pollAutofix,2000);
}

async function pollAutofix(){
  try{
    const d=await fetch('/api/code_review/autofix/status').then(r=>r.json());
    renderAutofixStatus(d);
    if(d.state!=='running'){
      clearInterval(_autofixPolling);_autofixPolling=null;
      const btn=$('cr-autofix-btn');
      btn.disabled=false;btn.textContent='▶ Run Auto-Fix';
    }
  }catch(e){console.error('autofix poll error',e);}
}

function renderAutofixStatus(d){
  const statusEl=$('cr-autofix-status');
  if(d.state==='running'){
    const n=d.results?d.results.length:0;
    statusEl.textContent=`Running… (${n} processed so far)`;
    statusEl.style.color='#ffd740';
  }else if(d.state==='done'){
    const fixed=(d.results||[]).filter(r=>r.status==='fixed').length;
    const total=(d.results||[]).length;
    const git=d.git||{};
    const gitMsg=git.pushed?` ✅ Pushed to GitHub — Railway redeploying`
      :git.message?` ⚠️ ${git.message}`:` (no git push)`;
    statusEl.innerHTML=`Done — ${fixed}/${total} fixes applied.<br><span style="font-size:.7rem;color:${git.pushed?'#00e676':'#ffd740'}">${gitMsg}</span>`;
    statusEl.style.color='#00e676';
  }else if(d.state==='error'){
    statusEl.textContent='Error: '+(d.error||'unknown');
    statusEl.style.color='#ff5252';
  }else{
    statusEl.textContent='';
  }

  const results=d.results||[];
  if(!results.length){
    $('cr-autofix-results').innerHTML='';
    return;
  }

  const statusIcon={fixed:'✅',skip:'⏭',error:'❌',pending:'⏳',info:'ℹ️'};
  const statusColor={fixed:'#00e676',skip:'#555',error:'#ff5252',pending:'#ffd740',info:'#90caf9'};

  $('cr-autofix-results').innerHTML=`<div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">`+
    results.map(r=>`
      <div style="display:flex;gap:10px;align-items:flex-start;padding:7px 10px;background:#111;border-radius:4px;border-left:3px solid ${statusColor[r.status]||'#555'}">
        <span style="font-size:.9rem;min-width:20px">${statusIcon[r.status]||'•'}</span>
        <div style="flex:1;min-width:0">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <span style="font-size:.68rem;font-weight:700;color:${statusColor[r.status]||'#555'}">${(r.status||'').toUpperCase()}</span>
            <span style="font-size:.68rem;color:#ffd740">${r.severity||''}</span>
            <span style="font-size:.68rem;color:#888;font-family:monospace">${r.file||''}</span>
          </div>
          <div style="font-size:.72rem;font-weight:600;margin-top:2px">${r.title||''}</div>
          <div style="font-size:.68rem;color:#888;margin-top:2px">${r.message||''}</div>
        </div>
      </div>`).join('')+
    `</div>`;
}

function renderResearchSignals(s){
  if(!s||!s.active_topics){return;}
  const topics=s.active_topics||[];
  $('res-active-topics').innerHTML=topics.length
    ? topics.map(t=>`<span style="background:#1a2744;color:#90caf9;padding:2px 8px;border-radius:10px;font-size:.65rem">${t}</span>`).join('')
    : '<span style="color:#555;font-size:.68rem">none</span>';
  $('res-signal-focus').textContent=s.strategy_focus||'none';
  $('res-signal-confidence').textContent=s.confidence||'--';
  const hints=s.param_hints||{};
  const hintStr=Object.keys(hints).length
    ? Object.entries(hints).map(([k,v])=>`${k}=${v}`).join(', ')
    : 'none';
  $('res-signal-params').textContent=hintStr;
}

let _currentProposalId=null;

function renderProposals(proposals){
  if(!proposals||!proposals.length){
    $('res-proposals').innerHTML='<div class="no-data">No proposals yet.</div>';
    return;
  }
  $('res-proposals').innerHTML=proposals.map(p=>`
    <div style="padding:10px 14px;background:#111;border-radius:6px;margin-bottom:8px;display:flex;gap:14px;align-items:flex-start">
      <div style="flex:1;min-width:0">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
          <span style="font-size:.72rem;font-weight:700;color:#90caf9">${p.class_name||'?'}</span>
          ${p.deployed?'<span style="font-size:.65rem;background:#1b5e20;color:#00e676;padding:1px 7px;border-radius:8px">deployed</span>':'<span style="font-size:.65rem;background:#1a237e;color:#90caf9;padding:1px 7px;border-radius:8px">proposed</span>'}
        </div>
        <div style="font-size:.72rem;font-weight:600;margin-bottom:2px">${p.finding_title||''}</div>
        <div style="font-size:.68rem;color:#888">${(p.finding_summary||'').substring(0,180)}${(p.finding_summary||'').length>180?'\u2026':''}</div>
      </div>
      <button onclick="viewProposal('${p.id}')" style="padding:5px 12px;background:#1a237e;color:#90caf9;border:none;border-radius:4px;cursor:pointer;font-size:.7rem;white-space:nowrap">View Code</button>
    </div>`).join('');
}

async function viewProposal(id){
  try{
    const d=await fetch('/api/research/proposals/'+id).then(r=>r.json());
    if(d.error){alert(d.error);return;}
    _currentProposalId=id;
    $('modal-title').textContent=d.class_name||'Strategy Proposal';
    $('modal-finding').textContent=d.finding_title+' \u2014 '+d.finding_summary;
    $('modal-code').textContent=d.code||'(no code)';
    $('modal-deploy-btn').disabled=!!d.deployed;
    $('modal-deploy-btn').textContent=d.deployed?'Already Deployed':'Deploy Strategy';
    $('modal-deploy-status').textContent=d.deployed?'Deployed \u2014 restart bot to activate':'';
    $('proposal-modal').style.display='block';
  }catch(e){alert('Failed to load proposal: '+e);}
}

function closeProposalModal(){
  $('proposal-modal').style.display='none';
  _currentProposalId=null;
}

async function deployProposal(){
  if(!_currentProposalId)return;
  if(!confirm('Deploy this strategy? It will be copied to src/strategies/ and hot-loaded into the running bot.'))return;
  const btn=$('modal-deploy-btn');
  btn.disabled=true;btn.textContent='Deploying\u2026';
  $('modal-deploy-status').textContent='';
  try{
    const r=await fetch('/api/research/proposals/'+_currentProposalId+'/deploy',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      $('modal-deploy-status').textContent='\u2705 Deployed to '+d.deployed_path+' \u2014 hot-loading\u2026';
      $('modal-deploy-status').style.color='#00e676';
      btn.textContent='Deployed';
    }else{
      $('modal-deploy-status').textContent='\u274c '+(d.error||'Unknown error');
      $('modal-deploy-status').style.color='#ff5252';
      btn.disabled=false;btn.textContent='Deploy Strategy';
    }
  }catch(e){
    $('modal-deploy-status').textContent='\u274c '+e;
    $('modal-deploy-status').style.color='#ff5252';
    btn.disabled=false;btn.textContent='Deploy Strategy';
  }
}

// ------------------------------------------------------------------ //
//  Agent countdown timers                                             //
// ------------------------------------------------------------------ //
let _timerData = null;
let _timerFetchedAt = 0;

function fmtCountdown(secs){
  if(secs==null)return'never run';
  if(secs<=0)return'<span style="color:#00e676">running soon</span>';
  const h=Math.floor(secs/3600);
  const m=Math.floor((secs%3600)/60);
  const s=Math.floor(secs%60);
  if(h>0)return h+'h '+String(m).padStart(2,'0')+'m';
  if(m>0)return m+'m '+String(s).padStart(2,'0')+'s';
  return String(s)+'s';
}

function fmtLastRun(ts){
  if(!ts)return'never';
  const diff=Math.round((Date.now()/1000)-ts);
  if(diff<60)return diff+'s ago';
  if(diff<3600)return Math.floor(diff/60)+'m ago';
  if(diff<86400)return Math.floor(diff/3600)+'h ago';
  return Math.floor(diff/86400)+'d ago';
}

function tickTimers(){
  if(!_timerData)return;
  const elapsed=(Date.now()/1000)-_timerFetchedAt;
  const agents=[
    {key:'meta_agent',   timerId:'timer-meta',     subId:'timer-meta-sub'},
    {key:'research',     timerId:'timer-research',  subId:'timer-research-sub'},
    {key:'code_review',  timerId:'timer-review',    subId:'timer-review-sub'},
  ];
  for(const {key,timerId,subId} of agents){
    const d=_timerData[key];
    if(!d)continue;
    const nextIn=d.next_in_secs!=null ? Math.max(0, d.next_in_secs - elapsed) : null;
    $(timerId).innerHTML=fmtCountdown(nextIn);
    $(subId).textContent='last run '+fmtLastRun(d.last_run);
  }
}

async function fetchAgentTimers(){
  try{
    _timerData=await fetch('/api/agent_timers').then(r=>r.json());
    _timerFetchedAt=Date.now()/1000;
    tickTimers();
  }catch(e){console.error('agent timers fetch failed',e);}
}

fetchAgentTimers();
setInterval(tickTimers,1000);
setInterval(fetchAgentTimers,60000);  // re-sync from server every minute

// ------------------------------------------------------------------ //
//  Code Review — run now button                                       //
// ------------------------------------------------------------------ //
let _reviewPollInterval=null;

async function runCodeReviewNow(){
  const btn=$('cr-runnow-btn');
  const status=$('cr-runnow-status');
  btn.disabled=true;
  status.textContent='Starting…';
  status.style.color='#ffd740';
  try{
    const r=await fetch('/api/code_review/run_now',{method:'POST'});
    const d=await r.json();
    if(!d.ok){
      status.textContent='Error: '+(d.error||'unknown');
      status.style.color='#ff5252';
      btn.disabled=false;
      return;
    }
  }catch(e){
    status.textContent='Request failed: '+e;
    status.style.color='#ff5252';
    btn.disabled=false;
    return;
  }
  status.textContent='Running — this takes ~60s…';
  if(_reviewPollInterval)clearInterval(_reviewPollInterval);
  _reviewPollInterval=setInterval(async()=>{
    try{
      const d=await fetch('/api/code_review/run_now/status').then(r=>r.json());
      if(!d.running){
        clearInterval(_reviewPollInterval);_reviewPollInterval=null;
        status.textContent='Done ✓ — refreshing results…';
        status.style.color='#00e676';
        btn.disabled=false;
        await fetchCodeReview();
        setTimeout(()=>{status.textContent='';},4000);
      }
    }catch(e){console.error('review poll error',e);}
  },3000);
}

// ------------------------------------------------------------------ //
//  Meta-Agent — run now button                                        //
// ------------------------------------------------------------------ //
let _maPollInterval=null;

async function runMetaAgentNow(){
  const btn=$('ma-runnow-btn');
  const status=$('ma-runnow-status');
  btn.disabled=true;
  status.textContent='Triggering…';
  status.style.color='#ffd740';

  // Record timestamp before trigger so we can detect a new run completing
  const triggerTs=Date.now()/1000;

  try{
    const r=await fetch('/api/meta-agent/run-now',{method:'POST'});
    const d=await r.json();
    if(!d.ok){
      status.textContent='Error: '+(d.error||'unknown');
      status.style.color='#ff5252';
      btn.disabled=false;
      return;
    }
  }catch(e){
    status.textContent='Request failed: '+e;
    status.style.color='#ff5252';
    btn.disabled=false;
    return;
  }

  status.textContent='Running — usually ~20s…';
  if(_maPollInterval)clearInterval(_maPollInterval);

  // Poll every 3s. Stop when last_run_ts > triggerTs (a new log was written)
  // OR when last_error is set (something went wrong before Claude was called).
  // This avoids the race condition where running=false before Claude even starts.
  _maPollInterval=setInterval(async()=>{
    try{
      const d=await fetch('/api/meta-agent/run-now/status').then(r=>r.json());
      if(d.last_run_ts>triggerTs){
        // New log written — done successfully
        clearInterval(_maPollInterval);_maPollInterval=null;
        btn.disabled=false;
        if(d.last_error){
          status.textContent='Error: '+d.last_error;
          status.style.color='#ff5252';
        }else{
          status.textContent='Done ✓ — results updated';
          status.style.color='#00e676';
          await fetchMeta();
          setTimeout(()=>{status.textContent='';},5000);
        }
      }else if(!d.running&&d.last_error&&d.last_error!=''){
        // Skipped before Claude was called (e.g. not enough trades)
        clearInterval(_maPollInterval);_maPollInterval=null;
        status.textContent='Skipped: '+d.last_error;
        status.style.color='#ffd740';
        btn.disabled=false;
      }
    }catch(e){console.error('meta-agent poll error',e);}
  },3000);
}

// ------------------------------------------------------------------ //
//  AI Intel tab                                                        //
// ------------------------------------------------------------------ //

function fmtProb(v){
  if(v==null)return '<span style="color:#555">--</span>';
  const pct=(v*100).toFixed(1);
  const col=v>=0.6?'#00e676':v<=0.4?'#ff5252':'#ffd740';
  return `<span style="color:${col}">${pct}%</span>`;
}

async function fetchEnsemble(){
  try{
    const d=await fetch('/api/ensemble/recent').then(r=>r.json());
    const status=$('ensemble-status');
    if(d.status!=='ok'){status.textContent=d.status;return;}
    status.textContent=`${d.cache_size} cached`;
    const tbody=$('ensemble-tbody');
    if(!d.evaluations||!d.evaluations.length){
      tbody.innerHTML='<tr><td colspan="5" style="color:#555;text-align:center">No evaluations yet</td></tr>';
      return;
    }
    tbody.innerHTML=d.evaluations.map(e=>`
      <tr>
        <td style="font-family:monospace;color:#90caf9">${e.condition_id}</td>
        <td>${fmtProb(e.claude_prob)}</td>
        <td>${fmtProb(e.openai_prob)}</td>
        <td>${fmtProb(e.consensus)}</td>
        <td style="color:#555">${e.age_minutes}m</td>
      </tr>`).join('');
  }catch(e){console.error('ensemble fetch error',e);}
}

async function fetchNews(){
  try{
    const d=await fetch('/api/news').then(r=>r.json());
    const statusEl=$('news-status');
    if(d.status!=='ok'){statusEl.textContent=d.status;return;}
    statusEl.textContent=`${d.headlines.length} headlines`;
    const list=$('news-list');
    if(!d.headlines||!d.headlines.length){
      list.innerHTML='<div style="color:#555;font-size:.72rem">No news loaded yet</div>';
      return;
    }
    const sentColor={positive:'#00e676',negative:'#ff5252',neutral:'#888'};
    const now=Date.now()/1000;
    list.innerHTML=d.headlines.map(h=>{
      const ageS=now-h._ts;
      const ageStr=ageS<3600?Math.round(ageS/60)+'m ago':Math.round(ageS/3600)+'h ago';
      const sent=h.sentiment||'neutral';
      const sentCol=sentColor[sent]||'#888';
      return `<div style="display:flex;gap:10px;align-items:flex-start;padding:6px 8px;background:#111;border-radius:4px">
        <span style="font-size:.65rem;padding:2px 6px;border-radius:3px;background:#1a1a1a;color:${sentCol};font-weight:700;min-width:48px;text-align:center;margin-top:1px">${sent}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:.75rem;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${h.title||''}</div>
          <div style="font-size:.65rem;color:#555;margin-top:2px">${h.source||''} &middot; ${ageStr}</div>
        </div>
      </div>`;
    }).join('');
  }catch(e){console.error('news fetch error',e);}
}

async function fetchHedges(){
  try{
    const d=await fetch('/api/hedges').then(r=>r.json());
    $('hedge-count').textContent=d.count||0;
    $('hedge-enabled').textContent=d.enabled?'YES':'NO';
    $('hedge-enabled').style.color=d.enabled?'#00e676':'#ff5252';
    $('hedge-status-badge').textContent=d.error?'error':'ok';
    const hedges=d.hedges||{};
    const tbody=$('hedge-tbody');
    const keys=Object.keys(hedges);
    if(!keys.length){
      tbody.innerHTML='<tr><td colspan="2" style="color:#555;text-align:center">No active hedges</td></tr>';
      return;
    }
    tbody.innerHTML=keys.map(k=>`
      <tr>
        <td style="font-family:monospace;color:#90caf9">${k.length>24?k.slice(0,24)+'…':k}</td>
        <td style="font-size:.68rem;color:#888">${JSON.stringify(hedges[k])}</td>
      </tr>`).join('');
  }catch(e){console.error('hedges fetch error',e);}
}

function updateQrBadge(logData){
  const badge=$('qr-active-badge');
  if(!badge)return;
  // Look for QuickResolution in cross-exchange log entries
  const hasQR=(logData||[]).some(e=>(e.strategy||'').toLowerCase().includes('quick'));
  badge.textContent=hasQR?'ACTIVE':'INACTIVE';
  badge.style.color=hasQR?'#00e676':'#555';
}

// ------------------------------------------------------------------ //
//  Compare tab                                                         //
// ------------------------------------------------------------------ //

function _fillCompareCard(prefix, bot){
  $(`compare-${prefix}-name`).textContent=bot.name||(prefix==='a'?'Bot A':'Bot B');
  $(`compare-${prefix}-bal`).textContent=bot.balance!=null?fmt(bot.balance):'--';
  const pnlEl=$(`compare-${prefix}-pnl`);
  const pnl=bot.total_pnl;
  pnlEl.textContent=pnl!=null?fmtPnl(pnl):'--';
  pnlEl.style.color=pnl!=null?(pnl>=0?'#00e676':'#ff5252'):'#888';
  const realEl=$(`compare-${prefix}-realized`);
  const rpnl=bot.realized_pnl;
  realEl.textContent=rpnl!=null?fmtPnl(rpnl):'--';
  realEl.style.color=rpnl!=null?(rpnl>=0?'#00e676':'#ff5252'):'#888';
  const roi=bot.pnl_pct;
  const roiEl=$(`compare-${prefix}-roi`);
  if(roi!=null){
    roiEl.textContent=(roi>=0?'+':'')+roi.toFixed(2)+'%';
    roiEl.style.color=roi>=0?'#00e676':'#ff5252';
  } else roiEl.textContent='--';
  const wr=bot.win_rate;
  $(`compare-${prefix}-wr`).textContent=wr!=null?(wr*100).toFixed(1)+'%':'--';
  $(`compare-${prefix}-fees`).textContent=bot.fees_paid!=null?fmt(bot.fees_paid):'--';
  $(`compare-${prefix}-exp`).textContent=bot.exposure!=null?fmt(bot.exposure):'--';
  $(`compare-${prefix}-pos`).textContent=bot.open_positions!=null?bot.open_positions:'--';
  $(`compare-${prefix}-trades`).textContent=bot.total_trades!=null?bot.total_trades:'--';
  $(`compare-${prefix}-tph`).textContent=bot.trades_per_hour!=null?bot.trades_per_hour:'--';
}

async function fetchCompare(){
  try{
    const d=await fetch('/api/compare').then(r=>r.json());
    const a=d.bot_a||{};
    const b=d.bot_b||{};

    $('compare-ts').textContent=d.ts?'Updated '+new Date(d.ts*1000).toLocaleTimeString():'--';

    // Bot A card
    _fillCompareCard('a', a);

    // Bot B card + warning
    const warn=$('compare-b-warning');
    const setup=$('compare-setup');
    const freshBadge=$('compare-b-fresh-badge');
    const noconfigBadge=$('compare-b-noconfig-badge');
    if(!b.configured){
      // PEER_BOT_URL not set — show not-configured state
      noconfigBadge.style.display='inline';
      freshBadge.style.display='none';
      warn.style.display='block';
      warn.textContent='Bot B not configured — set PEER_BOT_URL env var and restart';
      setup.style.display='block';
      ['bal','pnl','realized','roi','wr','fees','exp','pos','trades','tph'].forEach(f=>{
        const el=$(`compare-b-${f}`);
        if(el) el.textContent='--';
      });
    } else if(!b.available){
      // Configured but unreachable
      noconfigBadge.style.display='none';
      freshBadge.style.display='none';
      warn.style.display='block';
      warn.textContent='Bot B unreachable: '+(b.error||'connection failed');
      setup.style.display='none';
      ['bal','pnl','realized','roi','wr','fees','exp','pos','trades','tph'].forEach(f=>{
        const el=$(`compare-b-${f}`);
        if(el) el.textContent='--';
      });
    } else {
      warn.style.display='none';
      setup.style.display='none';
      noconfigBadge.style.display='none';
      freshBadge.style.display=b.fresh_start?'inline':'none';
      _fillCompareCard('b', b);
    }

    // Strategy breakdown table (PnL + trade counts)
    const sa=a.strategy_pnl||{};
    const sb=b.strategy_pnl||{};
    const ta=a.strategy_trades||{};
    const tb=b.strategy_trades||{};
    const strats=[...new Set([...Object.keys(sa),...Object.keys(sb)])].sort();
    const tbody=$('compare-strat-tbody');
    if(!strats.length){
      tbody.innerHTML='<tr><td colspan="5" style="color:#555;text-align:center">No strategy data yet</td></tr>';
    } else {
      tbody.innerHTML=strats.map(s=>{
        const ap=sa[s]; const bp=sb[s];
        const at=ta[s]; const bt=tb[s];
        const aP=ap!=null?`<span style="color:${ap>=0?'#00e676':'#ff5252'}">${fmtPnl(ap)}</span>`:'<span style="color:#444">--</span>';
        const bP=bp!=null?`<span style="color:${bp>=0?'#00e676':'#ff5252'}">${fmtPnl(bp)}</span>`:'<span style="color:#444">--</span>';
        const aT=at!=null?`<span style="color:#aaa">${at}</span>`:'<span style="color:#444">--</span>';
        const bT=bt!=null?`<span style="color:#aaa">${bt}</span>`:'<span style="color:#444">--</span>';
        return `<tr><td style="color:#ccc">${s}</td><td style="text-align:right">${aP}</td><td style="text-align:right">${aT}</td><td style="text-align:right">${bP}</td><td style="text-align:right">${bT}</td></tr>`;
      }).join('');
    }
  }catch(e){
    console.error('compare fetch error',e);
    const warn=$('compare-b-warning');
    if(warn){warn.style.display='block';warn.textContent='Fetch error: '+e.message;}
  }
}

// ── Sports tab ──────────────────────────────────────────────────────────── //

const BET_TYPE_LABELS = {
  game_winner:'Game Winner', over_under:'Over/Under', player_points:'Player Points',
  player_rebounds:'Player Rebounds', player_assists:'Player Assists',
  player_threes:'3-Pointers', player_double_double:'Double-Double',
  player_triple_double:'Triple-Double', player_other:'Player Prop',
  championship:'Championship', futures:'Futures', unknown:'Unknown',
};

const STATUS_ICON = {
  active: '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#00e676;margin-right:5px" title="In game"></span>',
  bench:  '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#ffd740;margin-right:5px" title="On bench"></span>',
  injured:'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#ff5252;margin-right:5px" title="Injured/Out"></span>',
  unknown:'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#555;margin-right:5px" title="Status unknown"></span>',
};

function loadSports(){
  fetch('/api/sports/stats').then(r=>r.json()).then(d=>{
    $('sp-total').textContent = d.total_trades ?? '--';
    const wrEl = $('sp-winrate');
    wrEl.textContent = (d.win_rate ?? '--') + (d.win_rate != null ? '%' : '');
    wrEl.style.color = d.win_rate >= 50 ? 'var(--green)' : 'var(--red)';
    const pEl = $('sp-pnl');
    pEl.textContent = fmtPnl(d.total_pnl);
    pEl.className = 'kpi-value ' + (d.total_pnl >= 0 ? 'green' : 'red');
    $('sp-open').textContent = d.open_count ?? '--';

    // Bet type table
    const btb = $('sp-bettype-tbody');
    if(d.bet_type_breakdown && d.bet_type_breakdown.length){
      btb.innerHTML = d.bet_type_breakdown.map(r=>`
        <tr>
          <td>${BET_TYPE_LABELS[r.bet_type]||r.bet_type}</td>
          <td style="text-align:right">${r.trades}</td>
          <td style="text-align:right;color:${r.win_rate>=50?'var(--green)':'var(--red)'}">${r.win_rate}%</td>
          <td style="text-align:right;color:${r.pnl>=0?'var(--green)':'var(--red)'}">${fmtPnl(r.pnl)}</td>
        </tr>`).join('');
    } else {
      btb.innerHTML='<tr><td colspan="4" style="color:var(--muted);text-align:center">No closed sports trades yet</td></tr>';
    }

    // League table
    const ltb = $('sp-league-tbody');
    if(d.league_breakdown && d.league_breakdown.length){
      ltb.innerHTML = d.league_breakdown.map(r=>`
        <tr>
          <td><span class="badge" style="background:var(--surface2)">${r.league}</span></td>
          <td style="text-align:right">${r.trades}</td>
          <td style="text-align:right;color:${r.win_rate>=50?'var(--green)':'var(--red)'}">${r.win_rate}%</td>
          <td style="text-align:right;color:${r.pnl>=0?'var(--green)':'var(--red)'}">${fmtPnl(r.pnl)}</td>
        </tr>`).join('');
    } else {
      ltb.innerHTML='<tr><td colspan="4" style="color:var(--muted);text-align:center">No data</td></tr>';
    }

    // Recent trades table
    const rtb = $('sp-recent-tbody');
    if(d.recent_trades && d.recent_trades.length){
      rtb.innerHTML = d.recent_trades.map(t=>`
        <tr>
          <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.market_question}">${t.market_question}</td>
          <td><span class="badge" style="background:var(--surface2);color:var(--muted)">${BET_TYPE_LABELS[t.bet_type]||t.bet_type}</span></td>
          <td>${t.league?t.league.toUpperCase():'--'}</td>
          <td>${t.outcome||'--'}</td>
          <td style="text-align:right;color:${t.pnl>=0?'var(--green)':'var(--red)'}">${fmtPnl(t.pnl)}</td>
          <td><span class="badge ${t.won?'green':'red'}">${t.won?'WIN':'LOSS'}</span></td>
        </tr>`).join('');
    } else {
      rtb.innerHTML='<tr><td colspan="6" style="color:var(--muted);text-align:center">No closed sports trades yet</td></tr>';
    }

    // Open positions with player detail (no live data yet — will be overlaid by loadSportsLive)
    const opDiv = $('sp-open-positions');
    if(d.open_positions && d.open_positions.length){
      opDiv.innerHTML = `<table class="trade-table">
        <thead><tr>
          <th>Market</th><th>Type</th><th>Outcome</th>
          <th style="text-align:right">Entry</th><th style="text-align:right">Current</th>
          <th style="text-align:right">Unr. PnL</th><th>Player Stats</th>
        </tr></thead>
        <tbody id="sp-open-tbody">` +
        d.open_positions.map(p=>`
          <tr id="sp-pos-${p.token_id.slice(-8)}">
            <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.market_question}">${p.market_question}</td>
            <td><span class="badge" style="background:var(--surface2);color:var(--muted)">${BET_TYPE_LABELS[p.bet_type]||p.bet_type}</span></td>
            <td>${p.outcome}</td>
            <td style="text-align:right">${(p.avg_cost*100).toFixed(1)}¢</td>
            <td style="text-align:right">${(p.current_price*100).toFixed(1)}¢</td>
            <td style="text-align:right;color:${p.unrealized_pnl>=0?'var(--green)':'var(--red)'}">${fmtPnl(p.unrealized_pnl)}</td>
            <td id="sp-player-${p.token_id.slice(-8)}" style="color:var(--muted);font-size:.75rem">${p.player_name||'--'}</td>
          </tr>`).join('') +
        `</tbody></table>`;
    } else {
      opDiv.innerHTML = '<div class="no-data" style="color:var(--muted)">No open sports positions</div>';
    }
  }).catch(e=>console.error('sports stats error',e));
}

function loadSportsLive(){
  fetch('/api/sports/live').then(r=>r.json()).then(d=>{
    // Live games section
    const lgDiv = $('sp-live-games');
    const badge = $('sp-live-badge');
    const liveGames = d.live_games || [];
    const recentGames = d.recent_games || [];
    const allDisplay = [...liveGames, ...recentGames.slice(0,10)];

    if(liveGames.length > 0){
      badge.style.display = 'inline';
    } else {
      badge.style.display = 'none';
    }

    if(allDisplay.length){
      lgDiv.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px">` +
        allDisplay.map(g=>{
          const isLive = g.is_live;
          const isFinal = g.is_final;
          const statusColor = isLive ? 'var(--red)' : isFinal ? 'var(--muted)' : 'var(--yellow)';
          const statusText = isLive ? (g.status_detail||'LIVE') : isFinal ? 'FINAL' : (g.status_detail||'SCHEDULED');
          return `<div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
              <span style="font-size:.65rem;font-weight:700;color:${statusColor}">${statusText}</span>
              <span style="font-size:.65rem;color:var(--muted)">${g.league}</span>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <div style="text-align:center;flex:1">
                <div style="font-size:.75rem;color:var(--text);font-weight:600">${g.away_abbr||g.away_team}</div>
                <div style="font-size:1.3rem;font-weight:700;color:${g.away_score>g.home_score?'var(--green)':'var(--text)'}">${g.away_score}</div>
              </div>
              <div style="color:var(--muted);font-size:.8rem;padding:0 8px">@</div>
              <div style="text-align:center;flex:1">
                <div style="font-size:.75rem;color:var(--text);font-weight:600">${g.home_abbr||g.home_team}</div>
                <div style="font-size:1.3rem;font-weight:700;color:${g.home_score>g.away_score?'var(--green)':'var(--text)'}">${g.home_score}</div>
              </div>
            </div>
          </div>`;
        }).join('') + `</div>`;
    } else {
      lgDiv.innerHTML = '<div class="no-data" style="color:var(--muted)">No live games right now</div>';
    }

    // Overlay player stats onto open positions
    const posIntel = d.position_intel || [];
    posIntel.forEach(pi=>{
      const tid = pi.token_id ? pi.token_id.slice(-8) : '';
      const cell = $('sp-player-'+tid);
      if(!cell) return;

      if(pi.player_stats && pi.player_stats.length){
        const ps = pi.player_stats[0];
        const icon = STATUS_ICON[ps.status] || STATUS_ICON.unknown;
        const inGame = ps.status === 'active';
        cell.innerHTML = `${icon}<strong style="color:${inGame?'var(--green)':'var(--muted)'}">${ps.name}</strong>
          <span style="color:var(--muted);font-size:.7rem;margin-left:6px">${ps.pts}pts ${ps.reb}reb ${ps.ast}ast (${ps.min})</span>`;
      } else if(pi.game){
        const g = pi.game;
        cell.innerHTML = `<span style="color:var(--muted);font-size:.75rem">${g.away_abbr} ${g.away_score}–${g.home_score} ${g.home_abbr} (${g.status_detail||g.status})</span>`;
      } else if(pi.player_name){
        cell.innerHTML = `<span style="color:var(--muted);">${pi.player_name} — no live data</span>`;
      }
    });

  }).catch(e=>console.error('sports live error',e));
}

fetchAll();
setInterval(fetchAll,3000);
</script>
</body>
</html>"""
