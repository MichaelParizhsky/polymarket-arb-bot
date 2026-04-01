"""
Strategy: Live Game — In-Play Momentum Betting.

Targets Polymarket game-winner markets where the game is CURRENTLY IN PROGRESS
and the market price hasn't fully converged to the in-game probability yet.

Why this works:
  - Polymarket is slow to reprice during live games (market makers are cautious)
  - ESPN computes a real-time win probability using score, time, possession
  - When ESPN says 82% and Polymarket says 73%, that's a 9% edge
  - The position resolves within hours at $1.00 — fast capital recycling
  - Risk is lower than pre-game bets because a large part of the game is over

Entry logic:
  - Find open game_winner markets matching live ESPN games
  - Game must be 40-90% complete (enough data, still enough payout upside)
  - ESPN win probability must diverge from Polymarket ask by >= MIN_DIVERGENCE
  - Score differential must be >= MIN_SCORE_DIFF (confirming leadership)
  - Polymarket price must be in [MIN_POLY_PRICE, MAX_POLY_PRICE] (tradeable zone)

Game completion calculation per sport:
  - NBA/CBB: 4 quarters × 12 min = 2880s (CBB: 2 halves × 20 min = 2400s)
  - NFL: 4 quarters × 15 min = 3600s
  - NHL: 3 periods × 20 min = 3600s
  - Soccer/MLS: 2 halves × 45 min = 5400s (clock counts up, not down)

ESPN win probability source:
  site.web.api.espn.com/apis/site/v2/sports/{sport}/summary?event={id}
  → winprobability[-1].homeWinPercentage (pre-computed by ESPN's model)
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from src.exchange.polymarket import Market, Orderbook
from src.sports.sports_intel import SportsIntel, classify_market, get_sports_intel
from src.strategies.base import BaseStrategy, Signal
from src.utils.constants import calc_taker_fee, MIN_TRADE_USDC
from src.utils.logger import logger
from src.utils.metrics import arb_opportunities, edge_detected


# ---------------------------------------------------------------------------
# Game completion helpers
# ---------------------------------------------------------------------------

# Total game seconds per league
_GAME_SECONDS: dict[str, int] = {
    "nba":   2880,   # 4 × 12 min
    "nfl":   3600,   # 4 × 15 min
    "nhl":   3600,   # 3 × 20 min
    "cbb":   2400,   # 2 × 20 min halves
    "ncaab": 2400,
    "mls":   5400,   # 2 × 45 min (clock counts up in soccer)
    "ncaaf": 3600,   # 4 × 15 min
}

# Periods per league
_PERIODS: dict[str, int] = {
    "nba": 4, "nfl": 4, "nhl": 3, "cbb": 2, "ncaab": 2, "mls": 2, "ncaaf": 4,
}

_CLOCK_RE = re.compile(r"(\d+):(\d{2})")


def _parse_clock_seconds(clock_str: str) -> int:
    """Parse 'MM:SS' game clock into seconds remaining in the period."""
    m = _CLOCK_RE.match(clock_str.strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # Try bare seconds
    try:
        return int(clock_str.strip())
    except ValueError:
        return 0


def _game_completion(game: dict) -> float:
    """
    Return fraction of game elapsed [0.0, 1.0].
    Uses ESPN's period + clock fields.
    Soccer uses minutes_elapsed directly.
    """
    league = game.get("league", "").lower()
    period = int(game.get("period", 0) or 0)
    clock = game.get("clock", "") or ""
    total_seconds = _GAME_SECONDS.get(league, 2880)
    total_periods = _PERIODS.get(league, 4)
    period_seconds = total_seconds // total_periods

    if league in ("mls",):
        # Soccer clock counts UP — clock is "87:00" style (elapsed time)
        elapsed_in_period = _parse_clock_seconds(clock)
        completed_periods = max(0, period - 1)
        elapsed = completed_periods * period_seconds + elapsed_in_period
    else:
        # Most sports: clock counts DOWN (time remaining in period)
        seconds_remaining_in_period = _parse_clock_seconds(clock)
        completed_periods = max(0, period - 1)
        elapsed_in_period = period_seconds - seconds_remaining_in_period
        elapsed = completed_periods * period_seconds + elapsed_in_period

    return min(max(elapsed / total_seconds, 0.0), 1.0)


def _score_diff(game: dict) -> int:
    """Return absolute score differential (home_score - away_score)."""
    return abs(int(game.get("home_score", 0) or 0) - int(game.get("away_score", 0) or 0))


def _leading_team_is_home(game: dict) -> bool | None:
    """Return True if home team leads, False if away leads, None if tied."""
    hs = int(game.get("home_score", 0) or 0)
    aw = int(game.get("away_score", 0) or 0)
    if hs > aw:
        return True
    if aw > hs:
        return False
    return None


def _team_matches(team_name: str, candidate: str) -> bool:
    """Fuzzy match: any significant word from team_name appears in candidate."""
    tn_words = {w.lower() for w in team_name.split() if len(w) > 2}
    cn_lower = candidate.lower()
    return bool(tn_words & {w for w in cn_lower.split() if len(w) > 2})


def _market_yes_is_home(question: str, game: dict) -> bool | None:
    """
    Determine if the YES outcome corresponds to the HOME team winning.
    Returns True = YES is home, False = YES is away, None = unknown.
    """
    classification = classify_market(question)
    team_a = classification.team_a  # the team in YES position ("Will [team_a] win")
    if not team_a:
        return None

    home_name = game.get("home_team", "")
    away_name = game.get("away_team", "")
    home_abbr = game.get("home_abbr", "")
    away_abbr = game.get("away_abbr", "")

    if _team_matches(team_a, home_name) or _team_matches(team_a, home_abbr):
        return True
    if _team_matches(team_a, away_name) or _team_matches(team_a, away_abbr):
        return False

    # Try the question text directly against home/away
    q_lower = question.lower()
    home_words = {w.lower() for w in home_name.split() if len(w) > 3}
    away_words = {w.lower() for w in away_name.split() if len(w) > 3}
    q_words = set(q_lower.split())

    home_score = len(home_words & q_words)
    away_score = len(away_words & q_words)

    if home_score > away_score:
        return True
    if away_score > home_score:
        return False

    # Check abbreviations
    if home_abbr.lower() in q_lower:
        return True
    if away_abbr.lower() in q_lower:
        return False

    return None


# ---------------------------------------------------------------------------
# LiveGameStrategy
# ---------------------------------------------------------------------------

class LiveGameStrategy(BaseStrategy):
    """
    In-play momentum strategy: buys the leading team's token during live games
    when Polymarket price lags ESPN's real-time win probability.

    Uses ESPN's pre-computed homeWinPercentage from the /summary endpoint —
    no custom model required. ESPN's model accounts for score, time, possession,
    and historical game dynamics for each sport.
    """

    def __init__(self, config, portfolio, risk_manager) -> None:
        super().__init__(config, portfolio, risk_manager)
        self._sports_intel: SportsIntel = get_sports_intel()
        # condition_id -> (entry_price, entered_at) — cooldown per market
        self._entered: dict[str, tuple[float, float]] = {}

    async def scan(self, context: dict[str, Any]) -> list[Signal]:
        markets: list[Market] = context.get("markets", [])
        orderbooks: dict[str, Orderbook] = context.get("orderbooks", {})
        signals: list[Signal] = []

        cfg = self.config.strategies
        min_game_pct   = getattr(cfg, "live_game_min_game_pct", 0.40)
        max_game_pct   = getattr(cfg, "live_game_max_game_pct", 0.90)
        min_divergence = getattr(cfg, "live_game_min_divergence", 0.06)
        min_score_diff = getattr(cfg, "live_game_min_score_diff", 5)
        min_poly_price = getattr(cfg, "live_game_min_poly_price", 0.55)
        max_poly_price = getattr(cfg, "live_game_max_poly_price", 0.88)
        max_spend      = getattr(cfg, "live_game_max_spend", 100.0)
        min_volume     = getattr(cfg, "live_game_min_volume", 500.0)
        cooldown_hours = getattr(cfg, "live_game_cooldown_hours", 2.0)

        # Prune stale cooldown entries
        cutoff = time.time() - cooldown_hours * 3600
        expired = [cid for cid, (_, ts) in self._entered.items() if ts < cutoff]
        for cid in expired:
            del self._entered[cid]

        # Pre-fetch all live games once for efficiency
        try:
            all_live = await asyncio.wait_for(
                self._sports_intel.get_all_live_games(), timeout=5.0
            )
        except Exception as exc:
            logger.warning(f"LiveGame: ESPN live fetch failed: {exc}")
            return signals

        live_games = [g for g in all_live if g.get("is_live")]
        if not live_games:
            logger.debug("LiveGame: no live games right now")
            return signals

        # Build lookup: (home_team_words | away_team_words) → game
        # We'll match per-market below

        skipped_not_game_winner = 0
        skipped_no_live_match = 0
        skipped_completion = 0
        skipped_score_diff = 0
        skipped_no_espn_prob = 0
        skipped_no_divergence = 0
        skipped_poly_price = 0
        skipped_cooldown = 0
        skipped_volume = 0

        for market in markets:
            if not market.active or market.closed:
                continue

            if (market.volume or 0.0) < min_volume:
                skipped_volume += 1
                continue

            if market.condition_id in self._entered:
                skipped_cooldown += 1
                continue

            # Only trade game_winner markets
            classification = classify_market(market.question)
            if classification.bet_type != "game_winner":
                skipped_not_game_winner += 1
                continue

            league = classification.league or ""

            # Find matching live game
            game = self._match_to_live_game(market.question, league, live_games)
            if not game:
                skipped_no_live_match += 1
                continue

            # Check game completion window
            pct = _game_completion(game)
            if pct < min_game_pct or pct > max_game_pct:
                skipped_completion += 1
                continue

            # Check score differential
            diff = _score_diff(game)
            if diff < min_score_diff:
                skipped_score_diff += 1
                continue

            # Get ESPN live win probability
            try:
                espn_home_wp = await asyncio.wait_for(
                    self._sports_intel.get_live_win_prob(
                        game["id"], game.get("league", "nba").lower()
                    ),
                    timeout=4.0,
                )
            except Exception:
                espn_home_wp = None

            if espn_home_wp is None:
                # Fallback: compute simple probability from score + time
                espn_home_wp = self._simple_win_prob(game)

            if espn_home_wp is None:
                skipped_no_espn_prob += 1
                continue

            # Determine which direction the market YES corresponds to
            yes_is_home = _market_yes_is_home(market.question, game)
            if yes_is_home is None:
                # Can't determine direction — skip
                skipped_no_espn_prob += 1
                continue

            # ESPN probability for the YES outcome
            espn_yes_prob = espn_home_wp if yes_is_home else (1.0 - espn_home_wp)

            # Only trade when ESPN thinks the YES side is the likely winner
            if espn_yes_prob < 0.55:
                # The NO side might be the opportunity — check it
                # (but we keep things simple: only buy the leading side)
                skipped_no_divergence += 1
                continue

            # Get YES token orderbook
            yes_token = next(
                (t for t in market.tokens if t.outcome.lower() == "yes"),
                market.tokens[0] if market.tokens else None,
            )
            if not yes_token:
                continue
            yes_book = orderbooks.get(yes_token.token_id)
            if not yes_book or yes_book.best_ask is None:
                continue

            ask = yes_book.best_ask
            if not (min_poly_price <= ask <= max_poly_price):
                skipped_poly_price += 1
                continue

            # Divergence check
            divergence = espn_yes_prob - ask
            if divergence < min_divergence:
                skipped_no_divergence += 1
                continue

            # Fee-adjusted edge
            fee = calc_taker_fee(ask, "sports")
            net_edge = (1.0 - ask) - fee
            if net_edge < getattr(cfg, "live_game_min_net_edge", 0.005):
                skipped_no_divergence += 1
                continue

            # Size the position
            size_usdc = self.risk.size_position(edge=net_edge, base_size=max_spend)
            if size_usdc < MIN_TRADE_USDC:
                continue

            ok, reason = self.risk.check_trade(
                yes_token.token_id, "BUY", size_usdc, "live_game"
            )
            if not ok:
                logger.debug(f"LiveGame risk blocked: {reason}")
                continue

            self._entered[market.condition_id] = (ask, time.time())
            arb_opportunities.labels(strategy="live_game").inc()
            edge_detected.labels(strategy="live_game").observe(net_edge)

            home_score = game.get("home_score", 0)
            away_score = game.get("away_score", 0)
            home_abbr = game.get("home_abbr", "?")
            away_abbr = game.get("away_abbr", "?")
            period = game.get("period", "?")
            clock = game.get("clock", "")

            self.log(
                f"[LIVE GAME] BUY YES @ {ask:.4f} | "
                f"ESPN={espn_yes_prob:.3f} poly={ask:.3f} div={divergence:.3f} | "
                f"{home_abbr} {home_score}-{away_score} {away_abbr} "
                f"Q{period} {clock} ({pct:.0%} done) | "
                f"edge={net_edge:.4f} size=${size_usdc:.2f} | {market.question[:55]}"
            )

            signals.append(Signal(
                strategy="live_game",
                token_id=yes_token.token_id,
                side="BUY",
                price=ask,
                size_usdc=size_usdc,
                edge=net_edge,
                notes=(
                    f"[LIVE_GAME] BUY YES @ {ask:.4f} | "
                    f"ESPN={espn_yes_prob:.3f} div={divergence:.3f} | "
                    f"{home_abbr} {home_score}-{away_score} {away_abbr} "
                    f"Q{period} {clock} ({pct:.0%})"
                ),
                metadata={
                    "outcome": "YES",
                    "espn_win_prob": espn_yes_prob,
                    "polymarket_ask": ask,
                    "divergence": divergence,
                    "game_pct": pct,
                    "score_diff": diff,
                    "home_score": home_score,
                    "away_score": away_score,
                    "period": period,
                    "clock": clock,
                    "home_team": game.get("home_team", ""),
                    "away_team": game.get("away_team", ""),
                    "league": league,
                    "condition_id": market.condition_id,
                    "entered_at": time.time(),
                },
            ))

        logger.info(
            f"LiveGame scan: {len(live_games)} live games | {len(signals)} signals | "
            f"skipped: {skipped_not_game_winner} not-game-winner, "
            f"{skipped_no_live_match} no-match, {skipped_completion} completion, "
            f"{skipped_score_diff} score-diff, {skipped_no_espn_prob} no-espn, "
            f"{skipped_no_divergence} no-div, {skipped_poly_price} price-range, "
            f"{skipped_cooldown} cooldown, {skipped_volume} volume"
        )
        return signals

    def _match_to_live_game(
        self, question: str, league: str, live_games: list[dict]
    ) -> dict | None:
        """Match a Polymarket question to a live ESPN game."""
        q_lower = question.lower()
        q_words = set(q_lower.split())

        # First pass: league-filtered games
        candidates = live_games
        if league:
            league_filtered = [g for g in live_games if g.get("league", "").lower() == league]
            if league_filtered:
                candidates = league_filtered

        best_match = None
        best_score = 0

        for game in candidates:
            home = game["home_team"].lower()
            away = game["away_team"].lower()
            home_abbr = game["home_abbr"].lower()
            away_abbr = game["away_abbr"].lower()

            home_words = {w for w in home.split() if len(w) > 3}
            away_words = {w for w in away.split() if len(w) > 3}

            score = len((home_words | away_words) & q_words)
            # Abbreviation match is a strong signal
            if home_abbr and home_abbr in q_lower:
                score += 2
            if away_abbr and away_abbr in q_lower:
                score += 2

            if score > best_score:
                best_score = score
                best_match = game

        return best_match if best_score >= 1 else None

    def _simple_win_prob(self, game: dict) -> float | None:
        """
        Fallback: compute win probability from score differential and time remaining.
        Uses Yale-derived coefficient (a=0.155) for NBA/CBB, scaled by time.
        Only used when ESPN /summary endpoint is unavailable.
        """
        import math

        league = game.get("league", "nba").lower()
        total_seconds = _GAME_SECONDS.get(league, 2880)
        pct = _game_completion(game)
        seconds_remaining = total_seconds * (1.0 - pct)

        home_score = int(game.get("home_score", 0) or 0)
        away_score = int(game.get("away_score", 0) or 0)
        score_diff = home_score - away_score  # positive = home leading

        if seconds_remaining <= 0:
            return 1.0 if score_diff > 0 else 0.0 if score_diff < 0 else 0.5

        # Yale coefficient for NBA — scales up as time decreases
        a = 0.155
        time_weight = math.sqrt(total_seconds) / (math.sqrt(seconds_remaining) + 1e-9)
        logit = a * time_weight * score_diff
        return 1.0 / (1.0 + math.exp(-logit))
