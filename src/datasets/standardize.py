"""Input-standardization layer for sequence models (plan Phase 4A -> 5/8).

Order of operations per feature dimension:
  1. log1p on configured heavy-tailed dimensions (inter-event times,
     sizes/volumes, event counts, ages) — applied to |x| with sign kept.
  2. robust standardization: (x - median) / MAD, statistics computed on
     the TRAINING SET ONLY (Phase 8 fits them; this module provides
     fit/apply/serialize) and reused byte-identically at inference
     (train/serve parity test).
  3. winsorization at ±`winsorize_bound` (config, default ±8); clip
     counts are reported for the Phase 10 drift monitor.

The fitted state serializes to plain JSON inside the model artifact
bundle (plan: weights + normalization state + feature list versioned
together).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# default log1p dimensions per input (heavy-tailed raw units)
LOG_DIMS = {
    "events": ["dt_s", "trade_n", "age_best_b_s", "age_best_a_s",
               "term_filled", "term_pulled", "refill_n"],
    "bars": ["duration_s", "n_events", "n_trades", "term_filled",
             "term_pulled", "refill_n", "events_per_s"],
    "tactical": ["events_inc"],
    "slow": ["events_inc", "failed_sweeps_l"],
    "regime": ["seconds_to_next_event", "seconds_since_last_event",
               "days_to_expiry", "days_since_roll"],
}


def _signed_log1p(x: np.ndarray) -> np.ndarray:
    return np.sign(x) * np.log1p(np.abs(x))


@dataclass
class InputStandardizer:
    """Per-input-tensor standardizer (one instance per model input)."""

    feature_names: list[str]
    log_features: list[str]
    winsorize_bound: float
    median: np.ndarray | None = None  # fitted (Phase 8)
    mad: np.ndarray | None = None

    def __post_init__(self) -> None:
        self._log_idx = np.array(
            [self.feature_names.index(f) for f in self.log_features
             if f in self.feature_names],
            dtype=np.int64,
        )

    # -- fitting (training set only; Phase 8 calls this) --------------------

    def fit(self, x: np.ndarray) -> "InputStandardizer":
        """x: [n, F] (or [n, T, F] — flattened over time). NaN-aware.

        Scale fallback chain for zero-inflated dimensions (sweep scores,
        depletion etc. are exactly 0 most seconds, making the plain MAD 0
        and clipping every nonzero value): MAD, else mean absolute
        deviation, else 1.0 (constant column -> zeros after centering)."""
        flat = x.reshape(-1, x.shape[-1]).astype(np.float64).copy()
        if len(self._log_idx):
            flat[:, self._log_idx] = _signed_log1p(flat[:, self._log_idx])
        med = np.nanmedian(flat, axis=0)
        dev = np.abs(flat - med)
        mad = np.nanmedian(dev, axis=0)
        meanad = np.nanmean(dev, axis=0)
        scale = np.where(mad > 1e-12, mad, np.where(meanad > 1e-12, meanad, 1.0))
        self.median = med
        self.mad = scale
        return self

    # -- application (identical in training and live) -----------------------

    def transform(self, x: np.ndarray) -> tuple[np.ndarray, int]:
        """Returns (standardized array, clipped-entry count)."""
        assert self.median is not None, "standardizer not fitted (Phase 8)"
        out = x.astype(np.float64).copy()
        if len(self._log_idx):
            out[..., self._log_idx] = _signed_log1p(out[..., self._log_idx])
        out = (out - self.median) / self.mad
        b = self.winsorize_bound
        clipped = int(np.sum((out < -b) | (out > b)))
        return np.clip(out, -b, b).astype(np.float32), clipped

    # -- serialization (model artifact bundle) -------------------------------

    def to_dict(self) -> dict:
        return {
            "feature_names": self.feature_names,
            "log_features": self.log_features,
            "winsorize_bound": self.winsorize_bound,
            "median": None if self.median is None else self.median.tolist(),
            "mad": None if self.mad is None else self.mad.tolist(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "InputStandardizer":
        s = cls(
            feature_names=list(d["feature_names"]),
            log_features=list(d["log_features"]),
            winsorize_bound=float(d["winsorize_bound"]),
        )
        if d["median"] is not None:
            s.median = np.asarray(d["median"], dtype=np.float64)
            s.mad = np.asarray(d["mad"], dtype=np.float64)
        return s

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict()))

    @classmethod
    def load(cls, path: Path) -> "InputStandardizer":
        return cls.from_dict(json.loads(path.read_text()))
