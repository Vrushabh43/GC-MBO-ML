"""Real-data verification of the Phase 6 sample index on a dev-slice
session: selection statistics, label invariants on real paths, hygiene
columns, uniqueness/effective-N behavior, and determinism.
"""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datasets.sample_index import build_session_samples  # noqa: E402
from features.flow_stream import replay_session_flow  # noqa: E402
from utilities.config import load_config  # noqa: E402

CFG = load_config()
SESSION = dt.date(2026, 1, 4)
NS = 1_000_000_000


@pytest.fixture(scope="module")
def built():
    cols = replay_session_flow(SESSION, CFG).cols
    df, summary = build_session_samples(SESSION, CFG, cols=cols)
    return cols, df, summary


class TestSelection:
    def test_counts_and_caps(self, built):
        _, df, summary = built
        by = summary["by_trigger"]
        assert by["clock"] > 1000  # ~1 Hz through the active session
        cap = CFG.raw["samples"]["event_cap_per_type_per_session"]
        assert all(v <= cap for k, v in by.items() if k != "clock")
        assert len(df) == sum(by.values())

    def test_min_spacing_globally_enforced(self, built):
        _, df, _ = built
        gaps = np.diff(df["ts"].to_numpy()) / NS
        assert (gaps >= float(CFG.raw["samples"]["min_spacing_s"]) - 1e-9).all()

    def test_no_cold_samples(self, built):
        _, df, _ = built
        # selection requires norm_ready, so sigma exists for every sample
        # whose window is complete (incomplete tail windows NaN all label
        # columns by design); sigma may be 0 in dead stretches
        complete = df[df["h30_window_complete"] == 1.0]
        assert complete["h30_sigma_pts"].notna().all()
        assert len(complete) > 3000


class TestLabelInvariantsOnRealPaths:
    def test_geometry(self, built):
        _, df, _ = built
        ok = df[df["h30_trainable"] == 1.0]
        assert len(ok) > 1000
        # high of the window is >= its low: upside + adverse_long >= 0
        # (both measured off the same entry)
        s = ok["h30_upside_pts"] + ok["h30_adverse_long_pts"]
        assert (s >= -1e-9).all()
        s2 = ok["h30_downside_pts"] + ok["h30_adverse_short_pts"]
        assert (s2 >= -1e-9).all()
        assert ((ok["h30_time_to_high_frac"] > 0) & (ok["h30_time_to_high_frac"] <= 1)).all()
        assert (ok["h30_cost_norm"] > 0).all()

    def test_longer_horizons_see_larger_moves(self, built):
        _, df, _ = built
        ok = df[(df["h30_trainable"] == 1.0) & (df["h600_trainable"] == 1.0)]
        # monotone envelope: the 10m window's best bid >= the 30s window's
        assert (
            ok["h600_upside_pts"] >= ok["h30_upside_pts"] - 1e-9
        ).all()

    def test_class_balance_not_degenerate(self, built):
        _, _, summary = built
        cb = summary["h30"]["class_balance"]
        assert 0.4 <= cb["no_trade"] < 1.0  # costs dominate at 30 s — but
        assert cb["bullish"] + cb["bearish"] > 0.02  # signal classes exist

    def test_direction_consistent_with_labels(self, built):
        _, df, _ = built
        ok = df[df["h30_trainable"] == 1.0]
        cost = 0.2
        bull = ok[ok["h30_direction"] == 1.0]
        assert (
            bull["h30_upside_pts"] - cost
            >= 2.0 * (bull["h30_adverse_long_pts"].clip(lower=0) + cost) - 1e-9
        ).all()


class TestHygieneAndWeights:
    def test_hygiene_columns(self, built):
        _, df, _ = built
        # Jan 4: no FOMC within any label window; calendar covers the era
        assert (df["release_policy"] == "ok").all()
        assert (df["calendar_uncovered"] == 0.0).all()
        assert set(df["crosses_session"].unique()) <= {0.0, 1.0}
        assert (df["label_end_ts"] == df["ts"] + 600 * NS).all()

    def test_uniqueness_and_effective_n(self, built):
        _, df, summary = built
        for h in (30, 120, 600):
            ok = df[df[f"h{h}_trainable"] == 1.0]
            w = ok[f"h{h}_uniqueness"]
            assert ((w > 0) & (w <= 1.0)).all()
            eff = summary[f"h{h}"]["effective_n"]
            assert 0 < eff < len(ok)
            assert eff == pytest.approx(float(w.sum()))
        # denser overlap at longer horizons -> smaller effective share
        e30 = summary["h30"]["effective_n"] / summary["h30"]["trainable"]
        e600 = summary["h600"]["effective_n"] / summary["h600"]["trainable"]
        assert e600 < e30

    def test_determinism(self, built):
        cols, df, summary = built
        df2, s2 = build_session_samples(SESSION, CFG, cols=cols)
        assert df.equals(df2)
        assert summary["by_trigger"] == s2["by_trigger"]
