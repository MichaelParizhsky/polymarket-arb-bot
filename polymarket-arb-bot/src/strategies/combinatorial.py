"""
strategies/combinatorial.py — Combinatorial / Logical Arbitrage

Finds pairs of markets that are logically related (e.g. "Trump wins 2024" and
"Republican wins 2024") where the prices imply a contradiction.

Uses sentence-transformers for semantic similarity matching to group related
markets, then checks for logical inconsistencies in their pricing.
"""
from __future__ import annotations
import os
from typing import List, Optional, Tuple, Dict
from itertools import combinations

import numpy as np
from loguru import logger

from src.models import Market, CombinatorialOpportunity, StrategyType, TradeDirection


MIN_PROFIT_PCT    = float(os.getenv("COMBINATORIAL_MIN_PROFIT_PCT",    "0.005"))
SIMILARITY_THRESH = float(os.getenv("COMBINATORIAL_SIMILARITY_THRESHOLD", "0.72"))
MAX_POSITION      = float(os.getenv("COMBINATORIAL_MAX_POSITION_USDC", "300"))
GAS_COST          = float(os.getenv("GAS_COST_ESTIMATE_USDC", "0.02"))

# Logical relationship patterns for rule-based detection
# (subset_keyword, superset_keyword, relationship_label)
LOGICAL_PATTERNS: List[Tuple[str, str, str]] = [
    ("afc",        "super bowl",    "afc_subset_superbowl"),
    ("nfc",        "super bowl",    "nfc_subset_superbowl"),
    ("republican", "wins",          "party_candidate"),
    ("democrat",   "wins",          "party_candidate"),
    ("btc",        "crypto",        "asset_sector"),
    ("ethereum",   "crypto",        "asset_sector"),
    ("rate cut",   "fed",           "policy_subset"),
    ("q1",         "annual",        "quarter_year"),
    ("q2",         "annual",        "quarter_year"),
]


class CombinatorialStrategy:
    """
    Two-phase scan:
    1. Embedding-based clustering (if sentence-transformers available)
       — groups semantically related markets
    2. Rule-based logical checks
       — finds price contradictions in related pairs

    Falls back to rule-based only if embeddings unavailable.
    """

    def __init__(self):
        self._embedder     = None
        self._embeddings   : Dict[str, np.ndarray] = {}
        self.opportunities_found = 0
        self._load_embedder()

    def _load_embedder(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("[Combinatorial] Loaded sentence-transformers embedder")
        except Exception as e:
            logger.warning(f"[Combinatorial] Embedder unavailable ({e}), using rule-based only")

    def scan(self, markets: List[Market]) -> List[CombinatorialOpportunity]:
        opps: List[CombinatorialOpportunity] = []

        # Only look at binary (yes/no) markets for combinatorial logic
        binary = [m for m in markets if len(m.outcomes) == 2]

        if self._embedder:
            opps.extend(self._embedding_scan(binary))
        opps.extend(self._rule_based_scan(binary))

        # Deduplicate by market pair
        seen:  set = set()
        unique: List[CombinatorialOpportunity] = []
        for o in opps:
            key = frozenset([o.market_a.market_id, o.market_b.market_id])
            if key not in seen:
                seen.add(key)
                unique.append(o)
                self.opportunities_found += 1

        if unique:
            logger.info(f"[Combinatorial] Found {len(unique)} opportunities")
        return sorted(unique, key=lambda o: o.profit_pct, reverse=True)

    # ── Embedding-based scan ──────────────────────────────────────────────────

    def _embedding_scan(self, markets: List[Market]) -> List[CombinatorialOpportunity]:
        if not markets:
            return []

        opps: List[CombinatorialOpportunity] = []
        questions = [m.question for m in markets]

        try:
            vecs = self._embedder.encode(questions, batch_size=64, show_progress_bar=False)
        except Exception as e:
            logger.warning(f"[Combinatorial] Encoding failed: {e}")
            return []

        for i, j in combinations(range(len(markets)), 2):
            sim = float(np.dot(vecs[i], vecs[j]) / (
                np.linalg.norm(vecs[i]) * np.linalg.norm(vecs[j]) + 1e-8
            ))
            if sim >= SIMILARITY_THRESH:
                opp = self._check_pair(markets[i], markets[j], sim, "semantic")
                if opp:
                    opps.append(opp)

        return opps

    # ── Rule-based scan ───────────────────────────────────────────────────────

    def _rule_based_scan(self, markets: List[Market]) -> List[CombinatorialOpportunity]:
        opps: List[CombinatorialOpportunity] = []
        index: Dict[str, List[Market]] = {}

        for pattern, superset_kw, label in LOGICAL_PATTERNS:
            for m in markets:
                q_lower = m.question.lower()
                if pattern in q_lower or superset_kw in q_lower:
                    index.setdefault(label, []).append(m)

        for label, group in index.items():
            for i, j in combinations(range(len(group)), 2):
                opp = self._check_pair(group[i], group[j], 0.5, label)
                if opp:
                    opps.append(opp)

        return opps

    # ── Pair evaluation ───────────────────────────────────────────────────────

    def _check_pair(
        self,
        m_a: Market,
        m_b: Market,
        similarity: float,
        relationship: str,
    ) -> Optional[CombinatorialOpportunity]:
        """
        Checks two binary markets for combinatorial pricing inconsistency.

        Example: If A ⊆ B logically (A winning implies B winning),
        then P(A_yes) should be ≤ P(B_yes).
        If P(A_yes) > P(B_yes) + threshold, that's a contradiction.

        Simplest exploitable version:
        Buy YES on the cheaper, Buy NO on the more expensive,
        where the combined cost < $1 after netting.
        """
        if len(m_a.outcomes) != 2 or len(m_b.outcomes) != 2:
            return None

        # Get YES prices for both markets
        yes_a = next((o.price for o in m_a.outcomes if "yes" in o.name.lower()), None)
        yes_b = next((o.price for o in m_b.outcomes if "yes" in o.name.lower()), None)
        if yes_a is None or yes_b is None:
            return None

        no_a = 1.0 - yes_a
        no_b = 1.0 - yes_b

        # Check: YES_a + NO_b < 1 (buy YES on A, buy NO on B)
        cost_1 = yes_a + no_b
        cost_2 = yes_b + no_a

        best_cost = min(cost_1, cost_2)
        if best_cost >= 1.0:
            return None

        gross_profit  = (1.0 - best_cost) * MAX_POSITION
        gas_total     = 2 * GAS_COST
        net_profit    = gross_profit - gas_total
        profit_pct    = net_profit / (best_cost * MAX_POSITION)

        if profit_pct < MIN_PROFIT_PCT:
            return None

        if cost_1 <= cost_2:
            legs = [
                {"market_id": m_a.market_id, "outcome": "YES", "direction": "buy", "price": yes_a},
                {"market_id": m_b.market_id, "outcome": "NO",  "direction": "buy", "price": no_b},
            ]
        else:
            legs = [
                {"market_id": m_b.market_id, "outcome": "YES", "direction": "buy", "price": yes_b},
                {"market_id": m_a.market_id, "outcome": "NO",  "direction": "buy", "price": no_a},
            ]

        logger.debug(
            f"[Combinatorial] {m_a.question[:40]} ↔ {m_b.question[:40]} | "
            f"sim={similarity:.2f} | profit={profit_pct*100:.2f}%"
        )

        return CombinatorialOpportunity(
            market_a    = m_a,
            market_b    = m_b,
            relationship= relationship,
            legs        = legs,
            profit_pct  = profit_pct,
            gross_profit= net_profit,
            similarity  = similarity,
        )
