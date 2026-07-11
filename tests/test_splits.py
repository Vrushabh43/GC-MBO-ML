"""Tests for the Phase 9 purged/embargoed split machinery, on synthetic
frames with known overlap and on the real dev-slice sample index.
"""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evaluation.splits import (  # noqa: E402
    Fold,
    frozen_segment,
    load_fold_frames,
    purge_train_samples,
    walk_forward_folds,
)
from utilities.config import load_config  # noqa: E402

CFG = load_config()
NS = 1_000_000_000


class TestFrozenOuterSplit:
    def test_boundaries_exact(self):
        assert frozen_segment(dt.date(2023, 12, 31), CFG) == "train"
        assert frozen_segment(dt.date(2024, 1, 1), CFG) == "validate"
        assert frozen_segment(dt.date(2024, 12, 31), CFG) == "validate"
        assert frozen_segment(dt.date(2025, 6, 1), CFG) == "test"
        assert frozen_segment(dt.date(2026, 1, 4), CFG) == "holdout"
        assert frozen_segment(dt.date(2017, 5, 21), CFG) == "train"


class TestWalkForward:
    DATES = [dt.date(2026, 1, 1) + dt.timedelta(days=k) for k in range(20)]

    def test_folds_chronological_with_embargo(self):
        folds = walk_forward_folds(self.DATES, 8, 3, CFG)
        assert len(folds) >= 3
        for f in folds:
            assert len(f.train_dates) == 8
            assert len(f.eval_dates) == 3
            assert len(f.embargo_dates) == 1  # config embargo_sessions
            assert max(f.train_dates) < min(f.embargo_dates)
            assert max(f.embargo_dates) < min(f.eval_dates)
        # walk-forward: consecutive folds advance
        assert folds[1].eval_dates[0] > folds[0].eval_dates[0]

    def test_never_a_single_split_api(self):
        # the fold generator is the only split API; a degenerate request
        # that cannot produce a full fold yields none
        assert walk_forward_folds(self.DATES[:5], 8, 3, CFG) == []

    def test_fold_rejects_nonchronological(self):
        with pytest.raises(AssertionError):
            Fold(
                train_dates=[dt.date(2026, 1, 5)],
                eval_dates=[dt.date(2026, 1, 4)],
                embargo_dates=[],
            )


class TestPurging:
    def frame(self):
        # samples every 100 s; label windows 600 s
        ts = np.arange(0, 100_000, 100, dtype=np.int64) * NS
        return pd.DataFrame(
            {
                "ts": ts,
                "label_end_ts": ts + 600 * NS,
                "h30_trainable": 1.0,
                "h30_uniqueness": 0.1,
            }
        )

    def test_overlapping_windows_purged(self):
        df = self.frame()
        eval_start = 50_000 * NS
        kept, audit = purge_train_samples(df, eval_start, CFG, 30)
        # windows must END before eval AND sample ts must respect the
        # 600 s minimum embargo
        assert (kept["label_end_ts"] < eval_start).all()
        assert kept["ts"].max() < eval_start - 600 * NS
        # the boundary samples were dropped
        assert audit["purged_or_embargoed"] > 0
        border = df[(df["ts"] >= eval_start - 600 * NS) & (df["ts"] < eval_start)]
        assert len(border) > 0
        assert not kept["ts"].isin(border["ts"]).any()

    def test_effective_n_in_audit(self):
        df = self.frame()
        kept, audit = purge_train_samples(df, 50_000 * NS, CFG, 30)
        assert audit["h30_effective_n"] == pytest.approx(0.1 * len(kept))


class TestOnRealIndex:
    def test_fold_over_dev_slice(self):
        from databento_io.sessions import list_session_dates

        dates = list_session_dates(
            CFG.data.dev_slice_start, CFG.data.dev_slice_end, CFG
        )
        folds = walk_forward_folds(dates, 8, 2, CFG)
        assert folds, "dev slice must yield at least one fold"
        train, ev, audit = load_fold_frames(folds[0], CFG, horizon_s=30)
        assert len(train) > 10_000 and len(ev) > 10_000
        # the invariant the whole module exists for:
        assert train["label_end_ts"].max() < ev["ts"].min()
        # one full session embargoed between train and eval
        assert audit["embargo_sessions_dropped"] == 1
        assert 0 < audit["h30_effective_n"] < audit["kept"]
        assert 0 < audit["eval_h30_effective_n"] < audit["eval_rows"]
        # eval sessions strictly after train sessions
        assert max(folds[0].train_dates) < min(folds[0].eval_dates)
