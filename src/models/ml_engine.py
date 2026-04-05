"""
Machine Learning Engine — self-improving calibration for Optimism Tax strategy.

Three learning layers:
1. BucketCalibrator  — online-updates the Becker price-bucket table with actual
                       resolved trade outcomes (Bayesian conjugate update).
2. CategoryTracker   — tracks real win/loss per category to refine edge bonuses.
3. WinPredictor      — LogisticRegression on full feature set after N≥50 samples.

MLEngine.update() is called after every resolved (SELL) trade.
MLEngine.get_blended_win_prob() blends Bayesian + ML predictions.
State persists in logs/ml_state.json across restarts.
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

import numpy as np

# Price buckets matching BayesianEngine calibration table
PRICE_BUCKETS: list[float] = [0.01, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 0.70, 0.80, 0.90]

CATEGORIES: list[str] = [
    "crypto", "weather", "sports", "politics",
    "finance", "entertainment", "world", "unknown",
]

# Minimum resolved trades before WinPredictor activates
N_MIN_PREDICTOR = 20

# Prior weight for Bayesian bucket blending (≈ virtual prior observations)
PRIOR_WEIGHT = 20


# ---------------------------------------------------------------------------
# BucketCalibrator
# ---------------------------------------------------------------------------

class BucketCalibrator:
    """Bayesian conjugate update of per-bucket actual win rates."""

    def __init__(self, base_table: dict[float, float]) -> None:
        self._base: dict[float, float] = dict(base_table)
        self._wins: dict[float, float] = {k: 0.0 for k in PRICE_BUCKETS}
        self._n: dict[float, int] = {k: 0 for k in PRICE_BUCKETS}

    def _nearest(self, yes_price: float) -> float:
        return min(PRICE_BUCKETS, key=lambda b: abs(b - yes_price))

    def update(self, yes_price: float, won: bool) -> None:
        bucket = self._nearest(yes_price)
        self._wins[bucket] += 1.0 if won else 0.0
        self._n[bucket] += 1

    def get_calibrated_rate(self, yes_price: float) -> Optional[float]:
        """Returns blended rate if ≥5 observations for this bucket, else None."""
        bucket = self._nearest(yes_price)
        n = self._n.get(bucket, 0)
        if n < 5:
            return None
        prior = self._base.get(bucket, yes_price)
        ml_wins = self._wins.get(bucket, 0.0)
        return (PRIOR_WEIGHT * prior + ml_wins) / (PRIOR_WEIGHT + n)

    def drift_table(self) -> dict:
        result = {}
        for bucket in PRICE_BUCKETS:
            base = self._base.get(bucket, bucket)
            n = self._n.get(bucket, 0)
            ml_rate = self.get_calibrated_rate(bucket)
            result[str(bucket)] = {
                "base_rate": round(base, 4),
                "ml_rate": round(ml_rate, 4) if ml_rate is not None else None,
                "n_obs": n,
                "drift_pp": round((ml_rate - base) * 100, 2) if ml_rate is not None else None,
            }
        return result


# ---------------------------------------------------------------------------
# CategoryTracker
# ---------------------------------------------------------------------------

class CategoryTracker:
    """Per-category win/loss tracking."""

    def __init__(self) -> None:
        self._wins: dict[str, int] = {}
        self._losses: dict[str, int] = {}

    def update(self, category: str, won: bool) -> None:
        if won:
            self._wins[category] = self._wins.get(category, 0) + 1
        else:
            self._losses[category] = self._losses.get(category, 0) + 1

    def win_rate(self, category: str) -> Optional[float]:
        w = self._wins.get(category, 0)
        l = self._losses.get(category, 0)
        n = w + l
        return w / n if n >= 5 else None

    def get_stats(self) -> dict:
        cats = set(list(self._wins.keys()) + list(self._losses.keys()))
        result = {}
        for cat in sorted(cats):
            w = self._wins.get(cat, 0)
            l = self._losses.get(cat, 0)
            n = w + l
            result[cat] = {
                "wins": w,
                "losses": l,
                "total": n,
                "win_rate_pct": round(w / n * 100, 1) if n else None,
            }
        return result


# ---------------------------------------------------------------------------
# WinPredictor
# ---------------------------------------------------------------------------

class WinPredictor:
    """Logistic regression trained on resolved trade features."""

    def __init__(self) -> None:
        self._X: list[list[float]] = []
        self._y: list[int] = []
        self._model = None
        self._scaler = None
        self._n_trained: int = 0

    def _featurize(self, meta: dict) -> list[float]:
        cat = meta.get("category", "unknown")
        cat_idx = CATEGORIES.index(cat) if cat in CATEGORIES else CATEGORIES.index("unknown")
        cat_onehot = [1.0 if i == cat_idx else 0.0 for i in range(len(CATEGORIES))]
        yes_ask = float(meta.get("yes_ask", 0.05))
        net_edge = float(meta.get("net_edge", 0.0))
        mc_p = float(meta.get("mc_p_profit", 0.9))
        true_no_prob = float(meta.get("true_no_prob", 0.95))
        return [yes_ask, net_edge, mc_p, true_no_prob, *cat_onehot]

    def add_sample(self, meta: dict, won: bool) -> None:
        try:
            self._X.append(self._featurize(meta))
            self._y.append(1 if won else 0)
        except Exception:
            pass

    def fit(self) -> bool:
        n = len(self._y)
        if n < N_MIN_PREDICTOR:
            return False
        if sum(self._y) < 2 or sum(1 - y for y in self._y) < 2:
            return False
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler

            X = np.array(self._X)
            y = np.array(self._y)
            self._scaler = StandardScaler()
            X_scaled = self._scaler.fit_transform(X)
            self._model = LogisticRegression(max_iter=1000, C=1.0, random_state=42)
            self._model.fit(X_scaled, y)
            self._n_trained = n
            return True
        except Exception:
            return False

    def predict(self, meta: dict) -> Optional[float]:
        if self._model is None or self._scaler is None:
            return None
        try:
            feat = np.array([self._featurize(meta)])
            feat_scaled = self._scaler.transform(feat)
            return float(self._model.predict_proba(feat_scaled)[0][1])
        except Exception:
            return None

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    @property
    def n_samples(self) -> int:
        return len(self._y)

    @property
    def accuracy(self) -> Optional[float]:
        if self._model is None or self._scaler is None or not self._X:
            return None
        try:
            from sklearn.metrics import accuracy_score
            X_scaled = self._scaler.transform(np.array(self._X))
            preds = self._model.predict(X_scaled)
            return round(float(accuracy_score(self._y, preds)) * 100, 1)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# MLEngine
# ---------------------------------------------------------------------------

class MLEngine:
    """Orchestrates all three learning layers."""

    def __init__(self, base_calibration: dict[float, float]) -> None:
        self.calibrator = BucketCalibrator(base_calibration)
        self.category_tracker = CategoryTracker()
        self.predictor = WinPredictor()
        self._total_updates: int = 0

    def update(self, trade_metadata: dict, won: bool) -> None:
        """Call after each position closes. metadata = Trade.metadata dict."""
        yes_ask = trade_metadata.get("yes_ask")
        category = trade_metadata.get("category", "unknown")

        if yes_ask is not None:
            self.calibrator.update(float(yes_ask), won)
        self.category_tracker.update(category, won)
        self.predictor.add_sample(trade_metadata, won)
        self._total_updates += 1

        # Refit every 10 new samples once threshold is reached
        if self._total_updates % 10 == 0 and self.predictor.n_samples >= N_MIN_PREDICTOR:
            self.predictor.fit()

    def get_blended_win_prob(self, meta: dict, bayesian_fallback: float) -> float:
        """
        Blend Bayesian estimate with ML prediction.
        ML weight ramps from 0 → 0.4 over the first 200 resolved samples.
        """
        ml_pred = self.predictor.predict(meta)
        if ml_pred is None:
            return bayesian_fallback
        n = self.predictor.n_samples
        ml_weight = min(0.4, n / 500.0)
        return ml_weight * ml_pred + (1.0 - ml_weight) * bayesian_fallback

    def get_updated_calibration_rate(self, yes_price: float) -> Optional[float]:
        return self.calibrator.get_calibrated_rate(yes_price)

    def get_stats(self) -> dict:
        return {
            "total_updates": self._total_updates,
            "predictor_samples": self.predictor.n_samples,
            "predictor_trained_on": self._n_trained_snapshot(),
            "predictor_ready": self.predictor.is_ready,
            "predictor_accuracy_pct": self.predictor.accuracy,
            "min_samples_needed": N_MIN_PREDICTOR,
            "calibration_drift": self.calibrator.drift_table(),
            "category_stats": self.category_tracker.get_stats(),
        }

    def _n_trained_snapshot(self) -> int:
        return self.predictor._n_trained

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str = "logs/ml_state.json") -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        state = {
            "total_updates": self._total_updates,
            "cal_wins": {str(k): v for k, v in self.calibrator._wins.items()},
            "cal_n": {str(k): v for k, v in self.calibrator._n.items()},
            "cat_wins": self.category_tracker._wins,
            "cat_losses": self.category_tracker._losses,
            "pred_X": self.predictor._X,
            "pred_y": self.predictor._y,
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, separators=(",", ":"))
        os.replace(tmp, path)

    def load(self, path: str = "logs/ml_state.json") -> bool:
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                state = json.load(f)
            self._total_updates = state.get("total_updates", 0)
            for k, v in state.get("cal_wins", {}).items():
                self.calibrator._wins[float(k)] = float(v)
            for k, v in state.get("cal_n", {}).items():
                self.calibrator._n[float(k)] = int(v)
            self.category_tracker._wins = state.get("cat_wins", {})
            self.category_tracker._losses = state.get("cat_losses", {})
            self.predictor._X = state.get("pred_X", [])
            self.predictor._y = state.get("pred_y", [])
            if self.predictor.n_samples >= N_MIN_PREDICTOR:
                self.predictor.fit()
            return True
        except Exception:
            return False
