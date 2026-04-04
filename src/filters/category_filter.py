"""
Category filter for the Optimism Tax strategy.

Based on Jon Becker's prediction market microstructure research:
- Maker edge varies significantly by market category
- HIGH: Crypto (+2.69pp), Weather (+2.57pp), Sports (+2.23pp)
- MEDIUM: Politics (+1.02pp)
- AVOID: Finance (+0.17pp — near-efficient)
- BONUS: Entertainment (+4.79pp), World Events (+7.32pp) — high edge, low liquidity
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Becker edge estimates in probability-point (pp) units, converted to floats
# (e.g. 2.69pp → 0.0269).  These represent the systematic maker advantage
# observed across each category in the microstructure study.
# ---------------------------------------------------------------------------
CATEGORY_EDGE: dict[str, float] = {
    "crypto": 0.0269,
    "weather": 0.0257,
    "sports": 0.0223,
    "politics": 0.0102,
    "finance": 0.0017,       # near-efficient — avoid
    "entertainment": 0.0479,
    "world": 0.0732,
    "unknown": 0.0050,       # conservative fallback
}

# ---------------------------------------------------------------------------
# Keyword lists used to detect each category from a market question string.
# Ordered from most-specific to least-specific within each category so the
# first regex match wins when the same token could match multiple categories.
# ---------------------------------------------------------------------------
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "crypto": [
        r"\bbtc\b", r"\bbitcoin\b", r"\beth\b", r"\bethereum\b",
        r"\bsol\b", r"\bsolana\b", r"\bcrypto\b", r"\bcoin\b",
        r"\btoken\b", r"\bdefi\b", r"\bnft\b", r"\bblockchain\b",
        r"\baltcoin\b", r"\bdoge\b", r"\bxrp\b", r"\bbnb\b",
        r"\bweb3\b", r"\bstablecoin\b", r"\busdc\b", r"\busdt\b",
    ],
    "weather": [
        r"\bweather\b", r"\bhurricane\b", r"\btornado\b", r"\bflood\b",
        r"\btemperature\b", r"\brainfall\b", r"\bsnow\b", r"\bstorm\b",
        r"\bcyclone\b", r"\bdrought\b", r"\bwildfire\b", r"\bearthquake\b",
        r"\btsunami\b", r"\bblizzard\b", r"\bheatwave\b",
    ],
    "sports": [
        r"\bnfl\b", r"\bnba\b", r"\bnhl\b", r"\bmlb\b", r"\bmls\b",
        r"\bsoccer\b", r"\bfootball\b", r"\bbasketball\b", r"\bbaseball\b",
        r"\bhockey\b", r"\btennis\b", r"\bgolf\b", r"\bmma\b", r"\bufc\b",
        r"\bolympic\b", r"\bworld cup\b", r"\bchampionship\b",
        r"\bplayoff\b", r"\bsuperbowl\b", r"\bsuper bowl\b",
        r"\bleague\b", r"\btournament\b", r"\bwinner\b.*\bgame\b",
    ],
    "politics": [
        r"\belection\b", r"\bpresident\b", r"\bsenate\b", r"\bcongress\b",
        r"\bprimary\b", r"\bvote\b", r"\bcandidate\b", r"\bpolitical\b",
        r"\bgovernor\b", r"\blegislat\b", r"\bpoll\b", r"\bballot\b",
        r"\bparty\b.*\belect\b", r"\bdemocrat\b", r"\brepublican\b",
        r"\bwhite house\b", r"\bcongressional\b", r"\bparliament\b",
    ],
    "entertainment": [
        r"\boxoffice\b", r"\bbox office\b", r"\bmovie\b", r"\bfilm\b",
        r"\boscars?\b", r"\bgrammy\b", r"\bemmy\b", r"\bawards?\b",
        r"\bnetflix\b", r"\bspotify\b", r"\balbum\b", r"\bsong\b",
        r"\bceleb\b", r"\bactor\b", r"\bactress\b", r"\bsinger\b",
        r"\btelevision\b", r"\bstreaming\b", r"\bbillboard\b",
    ],
    "finance": [
        r"\bstock\b", r"\bequity\b", r"\bfed\b", r"\binterest rate\b",
        r"\binflation\b", r"\bgdp\b", r"\brecession\b", r"\bmarket cap\b",
        r"\bearnings\b", r"\bipo\b", r"\bbond\b", r"\byield\b",
        r"\bsp500\b", r"\bs&p\b", r"\bnasdaq\b", r"\bdow jones\b",
        r"\bforex\b", r"\bcurrency\b.*\bexchange\b",
    ],
    "world": [
        r"\bwar\b", r"\bconflict\b", r"\binvasion\b", r"\bcease.?fire\b",
        r"\bunited nations\b", r"\bun\b.*\bsecurity council\b",
        r"\bgeopolit\b", r"\bsanction\b", r"\bdiplomat\b",
        r"\bnato\b", r"\bterror\b", r"\brefugee\b", r"\bcoup\b",
        r"\bhumanitarian\b", r"\btreaty\b", r"\bpeace\b.*\bdeal\b",
    ],
}

# Category detection order — more specific / higher-edge categories first.
_DETECTION_ORDER: list[str] = [
    "crypto",
    "weather",
    "sports",
    "entertainment",
    "world",
    "politics",
    "finance",
]


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation for consistent keyword matching."""
    return re.sub(r"[^\w\s]", " ", text.lower())


def classify_market(question: str, tags: list[str] | None = None) -> str:
    """Classify a Polymarket question into a research category.

    Parameters
    ----------
    question:
        The market question string (e.g. "Will BTC hit $100k by end of 2025?").
    tags:
        Optional list of platform-supplied tags (e.g. ["crypto", "bitcoin"]).
        Tags are appended to the search corpus and can accelerate detection.

    Returns
    -------
    str
        One of: "crypto", "weather", "sports", "politics", "finance",
        "entertainment", "world", "unknown".
    """
    if tags is None:
        tags = []

    corpus = _normalize(question + " " + " ".join(tags))

    for category in _DETECTION_ORDER:
        patterns = CATEGORY_KEYWORDS[category]
        for pattern in patterns:
            if re.search(pattern, corpus):
                return category

    return "unknown"


def get_category_edge(category: str) -> float:
    """Return the expected maker edge (in probability points) for a category.

    Parameters
    ----------
    category:
        A category name returned by :func:`classify_market`.

    Returns
    -------
    float
        Edge as a float in [0, 1].  For example, 0.0269 means a 2.69 pp edge.
        Returns the "unknown" fallback for unrecognised category strings.
    """
    return CATEGORY_EDGE.get(category, CATEGORY_EDGE["unknown"])


def is_tradeable_category(category: str, min_edge: float = 0.005) -> bool:
    """Return True when the category's maker edge meets the minimum threshold.

    Parameters
    ----------
    category:
        A category name returned by :func:`classify_market`.
    min_edge:
        Minimum acceptable edge (default 0.005 = 0.5 pp).

    Returns
    -------
    bool
        True if the category is worth trading from an edge perspective.

    Notes
    -----
    Finance (0.17 pp) sits below the default threshold and is excluded.
    Entertainment and World Events pass even at elevated thresholds.
    """
    edge = get_category_edge(category)
    return edge >= min_edge
