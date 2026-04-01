"""
Sports Intelligence Module.

Provides:
  1. Live game data from ESPN hidden API (free, no key needed)
  2. Player status (active / questionable / out / injured)
  3. Market-type classification (game_winner, over_under, player_points, player_rebounds, etc.)
  4. Back-to-back / rest-day detection
  5. Sportsbook divergence signal (optional — requires THE_ODDS_API_KEY env var)

All ESPN calls use httpx (already a project dependency) and are cached with a
short TTL so rapid dashboard refreshes don't hammer the endpoint.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.utils.logger import logger

# ---------------------------------------------------------------------------
# ESPN endpoint templates
# ---------------------------------------------------------------------------
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

_ESPN_SCOREBOARDS: dict[str, str] = {
    "nba":  f"{_ESPN_BASE}/basketball/nba/scoreboard",
    "nfl":  f"{_ESPN_BASE}/football/nfl/scoreboard",
    "nhl":  f"{_ESPN_BASE}/hockey/nhl/scoreboard",
    "mlb":  f"{_ESPN_BASE}/baseball/mlb/scoreboard",
    "cbb":  f"{_ESPN_BASE}/basketball/mens-college-basketball/scoreboard",
    "mls":  f"{_ESPN_BASE}/soccer/usa.1/scoreboard",
    "ncaaf": f"{_ESPN_BASE}/football/college-football/scoreboard",
}

_ESPN_SUMMARY_TPL = "{base}/{sport}/summary?event={event_id}"

# sport path per league (for summary endpoint)
_LEAGUE_SPORT_PATH: dict[str, str] = {
    "nba":   "basketball/nba",
    "nfl":   "football/nfl",
    "nhl":   "hockey/nhl",
    "mlb":   "baseball/mlb",
    "cbb":   "basketball/mens-college-basketball",
    "mls":   "soccer/usa.1",
    "ncaaf": "football/college-football",
}

# ---------------------------------------------------------------------------
# The Odds API (optional)
# ---------------------------------------------------------------------------
_ODDS_BASE = "https://api.the-odds-api.com/v4/sports"

_ODDS_SPORT_KEYS: dict[str, str] = {
    "nba":   "basketball_nba",
    "nfl":   "americanfootball_nfl",
    "nhl":   "icehockey_nhl",
    "mlb":   "baseball_mlb",
    "cbb":   "basketball_ncaab",
    "mls":   "soccer_usa_mls",
    "ncaaf": "americanfootball_ncaaf",
}


# ---------------------------------------------------------------------------
# Market classification
# ---------------------------------------------------------------------------
@dataclass
class MarketClassification:
    bet_type: str          # game_winner | over_under | player_points | player_rebounds |
                           # player_assists | player_threes | player_double_double |
                           # player_triple_double | player_other | championship | futures | unknown
    player_name: str = ""  # extracted player name if applicable
    team_a: str = ""
    team_b: str = ""
    prop_line: float = 0.0  # numeric line for over/under props
    over_under: str = ""    # "over" | "under" | ""
    league: str = ""

    @property
    def is_player_prop(self) -> bool:
        return self.bet_type.startswith("player_")

    @property
    def is_game_winner(self) -> bool:
        return self.bet_type == "game_winner"

    @property
    def conviction_penalty(self) -> float:
        """
        Additional conviction required above base threshold.
        Player props are harder to predict — require more certainty.
        """
        return {
            "game_winner": 0.00,
            "over_under": 0.04,
            "player_points": 0.06,
            "player_rebounds": 0.07,
            "player_assists": 0.07,
            "player_threes": 0.08,
            "player_double_double": 0.05,
            "player_triple_double": 0.05,
            "player_other": 0.08,
            "championship": -0.05,  # futures: lower bar ok
            "futures": -0.03,
            "unknown": 0.03,
        }.get(self.bet_type, 0.04)


# --- player name extraction ------------------------------------------------
# Extracts consecutive capitalized words (handles CamelCase like LeBron, McDermott)
_CAP_WORD_RE = re.compile(r"\b([A-Z][A-Za-z']{1,20})\b")
_NON_NAMES = frozenset([
    "Will", "The", "Los", "San", "New", "Las", "De", "El", "La",
    "Super", "Bowl", "NBA", "NFL", "NHL", "MLB", "MLS", "NCAA", "CBB",
    "March", "Madness", "Stanley", "Cup", "World", "Series", "Finals",
    "East", "West", "North", "South", "Game", "Season", "Round",
    "If", "In", "Is", "At", "By", "To",
])


def _extract_player_name(question: str) -> str:
    """
    Return the most-likely player name in the question, or ''.
    Uses a sliding window over capitalized words to find 2-word sequences
    that don't start with a known non-name word.
    """
    # Collect all (start_pos, word) pairs for capitalized words
    cap_words = [(m.start(), m.group(1)) for m in _CAP_WORD_RE.finditer(question)]

    # Walk through consecutive pairs (2-word names)
    for i in range(len(cap_words) - 1):
        pos1, w1 = cap_words[i]
        pos2, w2 = cap_words[i + 1]

        # Must be adjacent (only whitespace between them)
        gap = question[pos1 + len(w1):pos2].strip()
        if gap:  # non-whitespace between words → not consecutive
            continue

        if w1 in _NON_NAMES or w2 in _NON_NAMES:
            continue

        # Check for 3-word name (first + middle + last)
        if i + 2 < len(cap_words):
            pos3, w3 = cap_words[i + 2]
            gap2 = question[pos2 + len(w2):pos3].strip()
            if not gap2 and w3 not in _NON_NAMES and len(w3) > 1:
                return f"{w1} {w2} {w3}"

        return f"{w1} {w2}"

    return ""


def _extract_prop_line(question: str) -> float:
    """Extract the numeric threshold from a prop question (e.g. '25+ points' → 25.0)."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[\+\-]?\s*(?:points?|rebounds?|assists?|pts?|reb|ast)", question, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:over|under|more than|fewer than|at least)\s+(\d+(?:\.\d+)?)", question, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return 0.0


def _extract_teams(question: str) -> tuple[str, str]:
    """Very rough team extraction from 'X vs Y' or 'X beat Y' patterns."""
    m = re.search(r"([A-Z][a-zA-Z\s]+?)\s+(?:vs?\.?\s*|beat\s+|defeat\s+)([A-Z][a-zA-Z\s]+?)(?:\?|$|will|to\b)", question, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""


def _detect_league(question: str) -> str:
    q = question.lower()
    if any(k in q for k in ["nba", "basketball", "lakers", "celtics", "warriors", "bucks", "heat", "nuggets", "thunder", "pistons", "maple leafs" if False else "sixers"]):
        return "nba"
    if any(k in q for k in ["nfl", "super bowl", "quarterback", "touchdown", "chiefs", "eagles", "cowboys", "patriots"]):
        return "nfl"
    if any(k in q for k in ["nhl", "hockey", "stanley cup", "maple leafs", "bruins", "penguins", "capitals"]):
        return "nhl"
    if any(k in q for k in ["mlb", "baseball", "world series", "innings", "strikeout", "yankees", "dodgers", "red sox"]):
        return "mlb"
    if any(k in q for k in ["ncaa", "college basketball", "march madness", "cbb", "kenpom"]):
        return "cbb"
    if any(k in q for k in ["mls", "soccer", "fifa", "premier league", "la galaxy", "inter miami"]):
        return "mls"
    return ""


# Ordered list of (pattern, bet_type) — first match wins
_BET_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\btriple[\s-]double\b", re.I), "player_triple_double"),
    (re.compile(r"\bdouble[\s-]double\b", re.I), "player_double_double"),
    (re.compile(r"\b(?:score|pts?|points?)\s*\d+", re.I), "player_points"),
    (re.compile(r"\b\d+\s*(?:\+)?\s*(?:pts?|points?)\b", re.I), "player_points"),
    (re.compile(r"\brebounds?\b", re.I), "player_rebounds"),
    (re.compile(r"\breb\b", re.I), "player_rebounds"),
    (re.compile(r"\bassists?\b", re.I), "player_assists"),
    (re.compile(r"\bast\b", re.I), "player_assists"),
    (re.compile(r"\b(?:three[\s-]point(?:ers?)?|3[\s-]point(?:ers?)?|threes?|treys?)\b", re.I), "player_threes"),
    (re.compile(r"\b(?:blocks?|steals?|turnovers?|fouls?)\b", re.I), "player_other"),
    (re.compile(r"\b(?:game\s+total|total\s+points?|combined\s+score)\b", re.I), "over_under"),
    (re.compile(r"\b(?:championship|finals|super\s*bowl|stanley\s*cup|world\s*series|nba\s*finals)\b", re.I), "championship"),
    (re.compile(r"\b(?:playoffs?|qualify|make\s+the\s+playoffs|advance\s+to)\b", re.I), "futures"),
    (re.compile(r"\b(?:win\s+the\s+season|season\s+wins?|regular\s+season)\b", re.I), "futures"),
    (re.compile(r"\b(?:will\s+\w+\s+win|win\s+the\s+game|beat\s+the|defeat\s+the)\b", re.I), "game_winner"),
]

# If question has "over" or "under" + a number, it's over_under or player prop
_OVER_UNDER_RE = re.compile(r"\b(over|under|more than|fewer than|at least|at most)\b", re.I)
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


def classify_market(question: str) -> MarketClassification:
    """
    Classify a Polymarket sports market question into a structured bet type.
    Returns a MarketClassification with bet_type, player_name, teams, and prop line.
    """
    league = _detect_league(question)
    player_name = _extract_player_name(question)
    prop_line = _extract_prop_line(question)
    has_number = bool(_NUMBER_RE.search(question))
    has_over_under = bool(_OVER_UNDER_RE.search(question))
    team_a, team_b = _extract_teams(question)

    over_under = ""
    if has_over_under:
        m = _OVER_UNDER_RE.search(question)
        if m:
            kw = m.group(1).lower()
            over_under = "over" if kw in ("over", "more than", "at least") else "under"

    # Walk through ordered patterns
    for pattern, bet_type in _BET_TYPE_PATTERNS:
        if pattern.search(question):
            # Refine player props: if no player name found, it's probably a game-level stat
            if bet_type.startswith("player_") and not player_name:
                # Could be team stat question — treat as over_under
                if has_over_under and has_number:
                    bet_type = "over_under"
            return MarketClassification(
                bet_type=bet_type,
                player_name=player_name,
                team_a=team_a,
                team_b=team_b,
                prop_line=prop_line,
                over_under=over_under,
                league=league,
            )

    # Default fallback
    return MarketClassification(
        bet_type="unknown",
        player_name=player_name,
        team_a=team_a,
        team_b=team_b,
        prop_line=prop_line,
        over_under=over_under,
        league=league,
    )


# ---------------------------------------------------------------------------
# Simple TTL cache
# ---------------------------------------------------------------------------
class _TTLCache:
    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry and time.time() - entry[0] < self._ttl:
            return entry[1]
        return None

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# SportsIntel — main class
# ---------------------------------------------------------------------------
class SportsIntel:
    """
    Async sports intelligence client.
    Fetches live game data, player statuses, and sportsbook lines.
    Thread-safe via asyncio; designed to be instantiated once and shared.
    """

    def __init__(self) -> None:
        self._live_cache = _TTLCache(ttl_seconds=45.0)    # live scores refresh fast
        self._wp_cache = _TTLCache(ttl_seconds=30.0)       # win probability updates per play
        self._player_cache = _TTLCache(ttl_seconds=120.0)  # injury status changes slower
        self._odds_cache = _TTLCache(ttl_seconds=300.0)    # sportsbook lines refresh slowly
        self._odds_api_key = os.getenv("THE_ODDS_API_KEY", "")
        self._http_timeout = httpx.Timeout(8.0)

    # -----------------------------------------------------------------------
    # Live game scoreboard
    # -----------------------------------------------------------------------
    async def get_live_games(self, league: str = "nba") -> list[dict]:
        """
        Return list of current/recent games from ESPN scoreboard.
        Each dict has: id, name, status, period, clock, home_team, away_team,
        home_score, away_score, is_live.
        """
        cache_key = f"live:{league}"
        cached = self._live_cache.get(cache_key)
        if cached is not None:
            return cached

        url = _ESPN_SCOREBOARDS.get(league)
        if not url:
            return []

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning(f"SportsIntel ESPN scoreboard {league}: {exc}")
            return []

        games = []
        for event in data.get("events", []):
            comps = event.get("competitions", [{}])
            comp = comps[0] if comps else {}
            competitors = comp.get("competitors", [])

            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})

            status_obj = event.get("status", {})
            status_type = status_obj.get("type", {})
            status_name = status_type.get("name", "")
            status_detail = status_type.get("shortDetail", "")
            is_live = "IN_PROGRESS" in status_name or "HALFTIME" in status_name

            period = status_obj.get("period", 0)
            clock = status_obj.get("displayClock", "")

            games.append({
                "id": event.get("id", ""),
                "name": event.get("name", ""),
                "league": league.upper(),
                "status": status_name,
                "status_detail": status_detail,
                "is_live": is_live,
                "is_final": "FINAL" in status_name or "POST" in status_name,
                "period": period,
                "clock": clock,
                "home_team": home.get("team", {}).get("displayName", ""),
                "home_abbr": home.get("team", {}).get("abbreviation", ""),
                "home_score": int(home.get("score", 0) or 0),
                "away_team": away.get("team", {}).get("displayName", ""),
                "away_abbr": away.get("team", {}).get("abbreviation", ""),
                "away_score": int(away.get("score", 0) or 0),
            })

        self._live_cache.set(cache_key, games)
        return games

    async def get_all_live_games(self) -> list[dict]:
        """Fetch live games for all major leagues in parallel."""
        leagues = ["nba", "nfl", "nhl", "mlb", "cbb", "mls"]
        results = await asyncio.gather(
            *[self.get_live_games(lg) for lg in leagues],
            return_exceptions=True,
        )
        all_games: list[dict] = []
        for r in results:
            if isinstance(r, list):
                all_games.extend(r)
        return all_games

    # -----------------------------------------------------------------------
    # Player stats from live boxscore
    # -----------------------------------------------------------------------
    async def get_game_boxscore(self, game_id: str, league: str = "nba") -> dict:
        """
        Return player stats for a specific live game.
        Returns {home_team: str, away_team: str, players: [{name, team, pts, reb, ast, min, status}]}
        """
        cache_key = f"box:{league}:{game_id}"
        cached = self._live_cache.get(cache_key)
        if cached is not None:
            return cached

        sport_path = _LEAGUE_SPORT_PATH.get(league, "basketball/nba")
        url = f"{_ESPN_BASE}/{sport_path}/summary?event={game_id}"

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning(f"SportsIntel boxscore {league}/{game_id}: {exc}")
            return {}

        result: dict = {"home_team": "", "away_team": "", "players": []}

        # ESPN uses full stat names as keys, not abbreviations
        # keys: minutes, points, fieldGoalsMade-..., threePoint..., freeThrow...,
        #       rebounds, assists, turnovers, steals, blocks, offensiveRebounds,
        #       defensiveRebounds, fouls, plusMinus
        _KEY_MAP = {
            "minutes": "min",
            "points": "pts",
            "rebounds": "reb",
            "assists": "ast",
            "steals": "stl",
            "blocks": "blk",
            "turnovers": "to",
            "fouls": "pf",
            "fieldGoalsMade-fieldGoalsAttempted": "fg",
            "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "3pt",
        }

        boxscore = data.get("boxscore", {})
        for team_section in boxscore.get("players", []):
            team_name = team_section.get("team", {}).get("displayName", "")
            for stat_group in team_section.get("statistics", []):
                keys: list[str] = stat_group.get("keys", [])
                for athlete_entry in stat_group.get("athletes", []):
                    ath = athlete_entry.get("athlete", {})
                    name = ath.get("displayName", "")
                    stats_raw: list[str] = athlete_entry.get("stats", [])
                    did_not_play = athlete_entry.get("didNotPlay", False)
                    is_active = athlete_entry.get("active", False)
                    is_starter = athlete_entry.get("starter", False)
                    ejected = athlete_entry.get("ejected", False)

                    # Build stat dict using ESPN key names
                    stat_dict: dict[str, str] = {}
                    for raw_key, val in zip(keys, stats_raw):
                        mapped = _KEY_MAP.get(raw_key, raw_key)
                        stat_dict[mapped] = val or "0"

                    min_played = stat_dict.get("min", "0:00")
                    has_played = min_played not in ("0:00", "", "--", "0", "0:0")

                    if did_not_play:
                        player_status = "injured"
                    elif ejected:
                        player_status = "injured"  # ejected from game
                    elif has_played:
                        player_status = "active"   # played minutes → active/in-game
                    elif is_active:
                        player_status = "bench"    # on roster but no minutes yet
                    else:
                        player_status = "unknown"

                    def _safe_int(v: str) -> int:
                        try:
                            return int(str(v).split("-")[0].split(":")[0])
                        except (ValueError, IndexError):
                            return 0

                    result["players"].append({
                        "name": name,
                        "team": team_name,
                        "pts": _safe_int(stat_dict.get("pts", "0")),
                        "reb": _safe_int(stat_dict.get("reb", "0")),
                        "ast": _safe_int(stat_dict.get("ast", "0")),
                        "stl": _safe_int(stat_dict.get("stl", "0")),
                        "blk": _safe_int(stat_dict.get("blk", "0")),
                        "min": min_played,
                        "fg": stat_dict.get("fg", "--"),
                        "3pt": stat_dict.get("3pt", "--"),
                        "status": player_status,  # active | bench | injured | unknown
                        "starter": is_starter,
                        "dnp": did_not_play,
                    })

        self._live_cache.set(cache_key, result)
        return result

    # -----------------------------------------------------------------------
    # Player status (injury report from ESPN teams endpoint)
    # -----------------------------------------------------------------------
    async def get_player_statuses(self, team_abbr: str, league: str = "nba") -> list[dict]:
        """
        Return injury/status list for a team.
        Each dict: {name, status, description}
        """
        cache_key = f"injuries:{league}:{team_abbr}"
        cached = self._player_cache.get(cache_key)
        if cached is not None:
            return cached

        # ESPN team search
        sport_path = _LEAGUE_SPORT_PATH.get(league, "basketball/nba")
        url = f"{_ESPN_BASE}/{sport_path}/teams?limit=200"

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                teams_data = resp.json()
        except Exception as exc:
            logger.warning(f"SportsIntel teams {league}: {exc}")
            return []

        # Find team ID
        team_id = None
        for sport_entry in teams_data.get("sports", []):
            for league_entry in sport_entry.get("leagues", []):
                for team in league_entry.get("teams", []):
                    t = team.get("team", {})
                    if t.get("abbreviation", "").upper() == team_abbr.upper():
                        team_id = t.get("id")
                        break

        if not team_id:
            return []

        # Fetch injuries
        inj_url = f"{_ESPN_BASE}/{sport_path}/teams/{team_id}/injuries"
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(inj_url)
                resp.raise_for_status()
                inj_data = resp.json()
        except Exception as exc:
            logger.debug(f"SportsIntel injuries {league}/{team_abbr}: {exc}")
            return []

        players = []
        for item in inj_data.get("injuries", []):
            ath = item.get("athlete", {})
            players.append({
                "name": ath.get("displayName", ""),
                "status": item.get("status", ""),
                "description": item.get("longComment", item.get("shortComment", "")),
            })

        self._player_cache.set(cache_key, players)
        return players

    # -----------------------------------------------------------------------
    # Sportsbook divergence (The Odds API)
    # -----------------------------------------------------------------------
    async def get_sportsbook_implied_prob(
        self,
        event_description: str,
        league: str = "nba",
    ) -> float | None:
        """
        Return sportsbook-consensus implied probability for the YES outcome of
        the described event, or None if unavailable.

        Uses The Odds API (requires THE_ODDS_API_KEY).
        Returns a float in [0, 1] or None.
        """
        if not self._odds_api_key:
            return None

        cache_key = f"odds:{league}:{event_description[:60]}"
        cached = self._odds_cache.get(cache_key)
        if cached is not None:
            return cached

        sport_key = _ODDS_SPORT_KEYS.get(league)
        if not sport_key:
            return None

        url = f"{_ODDS_BASE}/{sport_key}/odds"
        params = {
            "apiKey": self._odds_api_key,
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "decimal",
        }

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                events = resp.json()
        except Exception as exc:
            logger.debug(f"SportsIntel Odds API {league}: {exc}")
            return None

        # Fuzzy match event_description against event home_team/away_team names
        desc_lower = event_description.lower()
        best_match = None
        best_score = 0

        for event in events:
            home = event.get("home_team", "").lower()
            away = event.get("away_team", "").lower()
            # Simple word-overlap scoring
            words = set(desc_lower.split())
            score = sum(1 for w in words if w in home or w in away)
            if score > best_score:
                best_score = score
                best_match = event

        if not best_match or best_score < 2:
            return None

        # Average implied prob for home team across bookmakers
        implied_probs = []
        for bookmaker in best_match.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    if outcome.get("name", "").lower() in best_match.get("home_team", "").lower():
                        odds = outcome.get("price", 1.0)
                        implied_probs.append(1.0 / odds if odds > 0 else 0.5)

        if not implied_probs:
            return None

        avg_prob = sum(implied_probs) / len(implied_probs)
        self._odds_cache.set(cache_key, avg_prob)
        return avg_prob

    # -----------------------------------------------------------------------
    # Convenience: find live game for a Polymarket question
    # -----------------------------------------------------------------------
    async def find_live_game_for_question(
        self, question: str, league: str = ""
    ) -> dict | None:
        """
        Try to match a Polymarket market question to a live ESPN game.
        Returns the game dict or None.
        """
        if not league:
            league = _detect_league(question) or "nba"

        games = await self.get_live_games(league)
        q_lower = question.lower()

        for game in games:
            home = game["home_team"].lower()
            away = game["away_team"].lower()
            home_abbr = game["home_abbr"].lower()
            away_abbr = game["away_abbr"].lower()

            # Check if any team name words appear in question
            home_words = set(home.split())
            away_words = set(away.split())
            q_words = set(q_lower.split())

            if (home_words & q_words) or (away_words & q_words):
                return game
            if home_abbr in q_lower or away_abbr in q_lower:
                return game

        return None

    # -----------------------------------------------------------------------
    # Live win probability from ESPN /summary endpoint
    # -----------------------------------------------------------------------
    async def get_live_win_prob(self, event_id: str, league: str = "nba") -> float | None:
        """
        Return ESPN's current home-team win probability for a live game.
        Uses the /summary endpoint which includes a 'winprobability' array —
        each entry has homeWinPercentage (float 0.0–1.0) per play.
        Returns the LAST entry (most recent play) or None on failure.

        Result is the HOME team win probability. Callers must invert for away.

        Cache TTL: 30s (live games update every few seconds).
        """
        cache_key = f"wp:{league}:{event_id}"
        cached = self._wp_cache.get(cache_key)
        if cached is not None:
            return cached

        sport_path = _LEAGUE_SPORT_PATH.get(league, "basketball/nba")
        # The /summary endpoint (site.web.api.espn.com) includes win probability data
        url = f"https://site.web.api.espn.com/apis/site/v2/sports/{sport_path}/summary?event={event_id}"

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.debug(f"SportsIntel win_prob {league}/{event_id}: {exc}")
            return None

        wp_arr = data.get("winprobability", [])
        if not wp_arr:
            return None

        # Last entry = most recent play's probability
        last_wp = wp_arr[-1]
        home_prob = last_wp.get("homeWinPercentage")
        if home_prob is None:
            return None

        result = float(home_prob)
        self._wp_cache.set(cache_key, result)
        return result

    # -----------------------------------------------------------------------
    # Back-to-back detection
    # -----------------------------------------------------------------------
    async def is_back_to_back(self, team_name: str, league: str = "nba") -> bool:
        """
        Return True if the team played a game yesterday (back-to-back).
        Uses ESPN schedule API. Returns False on any error.
        """
        sport_path = _LEAGUE_SPORT_PATH.get(league, "basketball/nba")
        url = f"{_ESPN_BASE}/{sport_path}/teams?limit=200"

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                teams_data = resp.json()
        except Exception:
            return False

        # Find team ID by name match
        team_id = None
        team_lower = team_name.lower()
        for sport_entry in teams_data.get("sports", []):
            for league_entry in sport_entry.get("leagues", []):
                for team in league_entry.get("teams", []):
                    t = team.get("team", {})
                    dn = t.get("displayName", "").lower()
                    nn = t.get("nickname", "").lower()
                    if team_lower in dn or team_lower in nn or dn in team_lower:
                        team_id = t.get("id")
                        break

        if not team_id:
            return False

        sched_url = f"{_ESPN_BASE}/{sport_path}/teams/{team_id}/schedule"
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(sched_url)
                resp.raise_for_status()
                sched = resp.json()
        except Exception:
            return False

        yesterday = time.time() - 86400
        for event in sched.get("events", []):
            event_time = event.get("date", "")
            try:
                import datetime
                et = datetime.datetime.fromisoformat(event_time.replace("Z", "+00:00")).timestamp()
                if abs(et - yesterday) < 43200:  # within 12h of yesterday
                    return True
            except Exception:
                continue

        return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_intel: SportsIntel | None = None


def get_sports_intel() -> SportsIntel:
    global _intel
    if _intel is None:
        _intel = SportsIntel()
    return _intel
