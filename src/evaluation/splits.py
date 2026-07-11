"""Phase 9 — purged and embargoed splits (plan: chronological ordering is
necessary but not sufficient; never random row-level shuffling).

Mechanics on the Phase 6 sample index:
  - PURGE: every training sample whose label window (`label_end_ts`)
    reaches into the evaluation period is removed.
  - EMBARGO: additionally drop the trailing buffer of the training period
    before evaluation begins — config `embargo_sessions` full sessions
    (plan recommendation; minimum one max label horizon, enforced as
    `min_embargo_s` when the session embargo is disabled).
  - WALK-FORWARD: evaluation always by folds, never a single split;
    effective sample sizes (Phase 6 uniqueness) reported per fold.
  - The OUTER split is frozen (v2.4): train 2017-2023 / validate 2024 /
    test 2025 / holdout Q1-2026 (touched once, at the very end — R7).

Sessions are identified by their trading dates (the sample files are
per-session); purging works on nanosecond timestamps within them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from utilities.config import Config

NS = 1_000_000_000


@dataclass(frozen=True)
class Fold:
    train_dates: list[dt.date]
    eval_dates: list[dt.date]
    embargo_dates: list[dt.date]  # dropped entirely (between train and eval)

    def __post_init__(self) -> None:
        if self.train_dates and self.eval_dates:
            assert max(self.train_dates) < min(self.eval_dates), "fold not chronological"


def frozen_segment(date: dt.date, cfg: Config) -> str:
    """The v2.4 frozen outer split. 'holdout' is touched ONCE, at the end."""
    s = cfg.raw["splits"]
    if date <= s["frozen_train_end"]:
        return "train"
    if date <= s["frozen_val_end"]:
        return "validate"
    if date <= s["frozen_test_end"]:
        return "test"
    return "holdout"


def walk_forward_folds(
    dates: list[dt.date],
    train_sessions: int,
    eval_sessions: int,
    cfg: Config,
    step_sessions: int | None = None,
) -> list[Fold]:
    """Chronological walk-forward folds over session dates with the
    configured session embargo between train and eval."""
    dates = sorted(dates)
    embargo = int(cfg.raw["splits"]["embargo_sessions"])
    step = step_sessions if step_sessions is not None else eval_sessions
    folds: list[Fold] = []
    i = 0
    while True:
        t0, t1 = i, i + train_sessions
        e0, e1 = t1 + embargo, t1 + embargo + eval_sessions
        if e1 > len(dates):
            break
        folds.append(
            Fold(
                train_dates=dates[t0:t1],
                eval_dates=dates[e0:e1],
                embargo_dates=dates[t1:e0],
            )
        )
        i += step
    return folds


def purge_train_samples(
    train: pd.DataFrame,
    eval_start_ts: int,
    cfg: Config,
    horizon_s: int,
) -> tuple[pd.DataFrame, dict]:
    """Apply purging (and the minimum time embargo when no session embargo
    separates the periods) to a training-sample frame.

    Returns (kept frame, audit dict). `label_end_ts` is the Phase 6 purge
    anchor (max horizon); purging with it is conservative for shorter
    horizons by construction.
    """
    n0 = len(train)
    min_embargo_ns = int(cfg.raw["splits"]["min_embargo_s"]) * NS
    cut = eval_start_ts - min_embargo_ns
    keep = (train["label_end_ts"].to_numpy() < eval_start_ts) & (
        train["ts"].to_numpy() < cut
    )
    kept = train[keep]
    audit = {
        "input_rows": n0,
        "purged_or_embargoed": int(n0 - len(kept)),
        "kept": int(len(kept)),
        f"h{horizon_s}_effective_n": float(
            kept.loc[kept[f"h{horizon_s}_trainable"] == 1.0, f"h{horizon_s}_uniqueness"].sum()
        ),
    }
    return kept, audit


def load_fold_frames(
    fold: Fold, cfg: Config, horizon_s: int = 30
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Load a fold's train/eval sample frames with purging applied and the
    plan-required effective-N audit. Only TRAINABLE rows (Phase 6 hygiene:
    complete window, in-session, release policy, warm sigma) survive."""
    from utilities.config import REPO_ROOT

    d = REPO_ROOT / cfg.raw["samples"]["sample_index_dir"]

    def load(dates: list[dt.date]) -> pd.DataFrame:
        frames = []
        for x in dates:
            p = d / f"samples-{x.strftime('%Y%m%d')}.parquet"
            df = pd.read_parquet(p)
            df["session_date"] = str(x)
            frames.append(df)
        return pd.concat(frames, ignore_index=True)

    train = load(fold.train_dates)
    ev = load(fold.eval_dates)
    train = train[train[f"h{horizon_s}_trainable"] == 1.0]
    ev = ev[ev[f"h{horizon_s}_trainable"] == 1.0]
    eval_start = int(ev["ts"].min())
    train, audit = purge_train_samples(train, eval_start, cfg, horizon_s)
    audit["eval_rows"] = int(len(ev))
    audit[f"eval_h{horizon_s}_effective_n"] = float(
        ev[f"h{horizon_s}_uniqueness"].sum()
    )
    audit["embargo_sessions_dropped"] = len(fold.embargo_dates)
    # invariant: no training label window reaches the evaluation period
    assert (train["label_end_ts"] < eval_start).all()
    return train, ev, audit
