"""
Crypto market detection utility.

Identifies whether a Polymarket prediction market is about a crypto asset price
by matching keywords in the question text and/or tags against known Binance symbols.
"""
from __future__ import annotations

# Mirrors SYMBOL_MAP in src/exchange/binance.py.
# Keep in sync if new assets are added to BinanceFeed.
_KEYWORD_TO_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT",
    "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT",
    "ethereum": "ETHUSDT",
    "sol": "SOLUSDT",
    "solana": "SOLUSDT",
    "bnb": "BNBUSDT",
    "doge": "DOGEUSDT",
    "dogecoin": "DOGEUSDT",
    "xrp": "XRPUSDT",
    "ripple": "XRPUSDT",
    "matic": "MATICUSDT",
    "polygon": "MATICUSDT",
    "link": "LINKUSDT",
    "chainlink": "LINKUSDT",
    "avax": "AVAXUSDT",
    "avalanche": "AVAXUSDT",
}

# Price-related words that strengthen a positive match when found alongside a keyword.
# Absence of these does NOT block detection — the keyword alone is sufficient.
_PRICE_HINTS: frozenset[str] = frozenset({
    "price", "above", "below", "reach", "hit", "exceed",
    "surpass", "close", "trade", "end", "finish",
    "$", "k", "usd", "usdc",
})


def detect_crypto_symbol(
    market_question: str,
    market_tags: list[str],
) -> str | None:
    """
    Return the Binance symbol (e.g. ``'BTCUSDT'``) if this market is a crypto
    price prediction market, or ``None`` if it is not.

    Detection logic
    ---------------
    1. Tokenise the question (lower-case, split on non-alphanumeric boundaries).
    2. Check every token and every tag against the keyword map.
    3. Return the mapped Binance symbol on the first match.

    Supported assets: BTC, ETH, SOL, XRP, BNB, DOGE, LINK, AVAX, MATIC.

    Parameters
    ----------
    market_question:
        The full question text from the Polymarket market, e.g.
        "Will BTC be above $100k by end of January?".
    market_tags:
        List of tag strings from the market, e.g. ["crypto", "btc", "bitcoin"].

    Returns
    -------
    str | None
        Binance symbol string on match, ``None`` otherwise.

    Examples
    --------
    >>> detect_crypto_symbol("Will BTC be above $100k?", [])
    'BTCUSDT'
    >>> detect_crypto_symbol("Will ETH reach $5000?", ["crypto"])
    'ETHUSDT'
    >>> detect_crypto_symbol("Will the Fed cut rates?", ["economics"])
    None
    """
    # Check tags first — they are already clean keywords
    for tag in market_tags:
        symbol = _KEYWORD_TO_SYMBOL.get(tag.lower().strip())
        if symbol:
            return symbol

    # Tokenise question: split on anything that is not a letter or digit
    import re
    tokens = re.split(r"[^a-zA-Z0-9]+", market_question.lower())
    for token in tokens:
        if not token:
            continue
        symbol = _KEYWORD_TO_SYMBOL.get(token)
        if symbol:
            return symbol

    return None
