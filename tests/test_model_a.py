"""Tests for the Step 20 Model A gate machinery: dependency-free metric
known-answers, criteria logic, loading/thinning rules, and an end-to-end
train+evaluate on the real dev-slice index (plumbing + determinism — the
dev slice is NOT the gate; the gate runs on the registered OOS period).
"""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from models.model_a import (  # noqa: E402
    FEATURE_EXCLUDE,
    evaluate_gate,
    family_of,
    feature_columns,
    load_index_frame,
    multiclass_brier,
    rank_auc,
    train_model_a,
)
from utilities.config import load_config  # noqa: E402

CFG = load_config()


class TestMetrics:
    def test_rank_auc_known(self):
        y = np.array([0, 0, 1, 1])
        assert rank_auc(y == 1, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
        assert rank_auc(y == 1, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
        assert rank_auc(y == 1, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
        # one inversion among 2x2 pairs -> 0.75
        assert rank_auc(y == 1, np.array([0.1, 0.8, 0.2, 0.9])) == 0.75

    def test_brier_known(self):
        proba = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        assert multiclass_brier(proba, np.array([0, 1])) == 0.0
        assert multiclass_brier(proba, np.array([1, 0])) == pytest.approx(2.0)

    def test_gate_criteria_known_pass_and_fail(self):
        n = 4000
        rng = np.random.default_rng(0)
        y = rng.integers(0, 3, n)
        # oracle probabilities -> perfect AUC; returns aligned with class
        proba = np.full((n, 3), 0.05)
        proba[np.arange(n), y] = 0.9
        ret = np.where(y == 1, 1.0, np.where(y == 2, -1.0, 0.02 * rng.standard_normal(n)))
        sessions = np.repeat([f"d{k}" for k in range(25)], n // 25)
        oos = pd.DataFrame(
            {
                "h30_direction": y,
                "h30_final_return_pts": ret,
                "session_date": sessions,
                "release_policy": "ok",
            }
        )
        freq = np.bincount(y, minlength=3) / n
        ev = evaluate_gate(proba, oos, freq, CFG, "oracle")
        assert ev.passed and ev.auc_mean > 0.99 and ev.expectancy_pts > 0

        # anti-oracle: directional probs random, returns adversarial
        proba2 = np.full((n, 3), 1 / 3)
        ev2 = evaluate_gate(proba2, oos, freq, CFG, "uninformed")
        assert not ev2.passed
        assert not ev2.criteria["statistical_auc"]

    def test_concentration_criterion(self):
        n = 2500
        y = np.ones(n, dtype=int)  # all bullish
        proba = np.zeros((n, 3)); proba[:, 1] = 0.9
        ret = np.full(n, -0.1)  # losing everywhere...
        sessions = np.repeat([f"d{k}" for k in range(25)], n // 25)
        ret[sessions == "d0"] = 50.0  # ...except one bonanza session
        oos = pd.DataFrame(
            {
                "h30_direction": y,
                "h30_final_return_pts": ret,
                "session_date": sessions,
                "release_policy": "ok",
            }
        )
        ev = evaluate_gate(proba, oos, np.array([0.1, 0.8, 0.1]), CFG, "conc")
        assert ev.expectancy_pts > 0  # looks great in aggregate
        assert not ev.criteria["robust_wo_best_session"]  # caught
        assert not ev.passed


class TestLoading:
    def test_thinning_keeps_event_samples(self):
        d = dt.date(2026, 1, 6)
        full = load_index_frame([d], CFG)
        thin = load_index_frame([d], CFG, thin_clock=2000)
        ev_full = (full["trigger"] != "clock").sum()
        ev_thin = (thin["trigger"] != "clock").sum()
        assert ev_thin == ev_full  # event-triggered rows never thinned
        assert (thin["trigger"] == "clock").sum() <= 2000
        assert len(thin) < len(full)

    def test_feature_columns_exclude_price_level(self):
        df = load_index_frame([dt.date(2026, 1, 4)], CFG)
        feats = feature_columns(df)
        assert "f_mid_ticks" not in feats
        assert len(feats) >= 80
        for c in FEATURE_EXCLUDE:
            assert c not in feats

    def test_families_cover_features(self):
        df = load_index_frame([dt.date(2026, 1, 4)], CFG)
        feats = feature_columns(df)
        unassigned = [f for f in feats if family_of(f) == "other"]
        assert not unassigned, unassigned


class TestEndToEndPlumbing:
    @pytest.fixture(scope="class")
    def small(self):
        train_dates = [dt.date(2026, 1, d) for d in (5, 6, 7, 8)]
        oos_dates = [dt.date(2026, 1, d) for d in (12, 13)]
        train = load_index_frame(train_dates, CFG, thin_clock=4000)
        oos = load_index_frame(oos_dates, CFG)
        return train, oos

    def test_train_predict_evaluate(self, small):
        train, oos = small
        model, feats = train_model_a(train, CFG, weighted=False)
        proba = model.predict(oos[feats].to_numpy(np.float32))
        assert proba.shape == (len(oos), 3)
        assert np.allclose(proba.sum(axis=1), 1.0, atol=1e-6)
        freq = np.bincount(train["h30_direction"].astype(int), minlength=3) / len(train)
        ev = evaluate_gate(proba, oos, freq, CFG, "dev-plumbing")
        # plumbing assertions only — the dev slice is not the gate
        assert 0.0 <= ev.auc_mean <= 1.0
        assert ev.n_trades > 0
        assert isinstance(ev.passed, bool)

    def test_training_determinism(self, small):
        train, oos = small
        m1, feats = train_model_a(train, CFG, weighted=False)
        m2, _ = train_model_a(train, CFG, weighted=False)
        X = oos[feats].to_numpy(np.float32)[:500]
        assert np.array_equal(m1.predict(X), m2.predict(X))

    def test_uniqueness_weighting_changes_model(self, small):
        train, oos = small
        m1, feats = train_model_a(train, CFG, weighted=False)
        m2, _ = train_model_a(train, CFG, weighted=True)
        X = oos[feats].to_numpy(np.float32)[:2000]
        assert not np.array_equal(m1.predict(X), m2.predict(X))
