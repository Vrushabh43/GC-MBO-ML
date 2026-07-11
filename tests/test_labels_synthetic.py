"""Known-answer tests for Phase 6 labels (Step 18) and the sample index
machinery (Step 19): tradeable-price label math on constructed paths,
direction classes under the explicit cost model, dual-unit normalization,
hygiene flags, uniqueness weights, trigger selection, spacing, and
determinism.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from calendar_mod.events import EventCalendar, ScheduledEvent  # noqa: E402
from datasets.sample_index import (  # noqa: E402
    build_session_samples,
    uniqueness_weights,
)
from labeling.labels import (  # noqa: E402
    DIR_BEARISH,
    DIR_BULLISH,
    DIR_NO_TRADE,
    LabelEngine,
    build_second_path,
    SecondPath,
)
from utilities.config import load_config  # noqa: E402

CFG = load_config()
NS = 1_000_000_000
PX = 1_000_000_000  # 1 point in fixed-point


def path_from_quotes(quotes: list[tuple[float, float]], sec0: int = 1000) -> SecondPath:
    """One (bid_pts, ask_pts) pair per second -> SecondPath via the real
    builder (each second gets one valid row)."""
    n = len(quotes)
    cols = {
        "ts": np.array([(sec0 + k) * NS + 5 for k in range(n)], dtype=np.uint64),
        "valid": np.ones(n, dtype=np.uint8),
        "bid_px": np.array([int(b * PX) for b, _ in quotes], dtype=np.int64),
        "ask_px": np.array([int(a * PX) for _, a in quotes], dtype=np.int64),
    }
    return build_second_path(cols)


class TestPathConstruction:
    def test_extremes_and_carry_forward(self):
        cols = {
            "ts": np.array([1000 * NS, 1000 * NS + 5000, 1003 * NS], dtype=np.uint64),
            "valid": np.array([1, 1, 1], dtype=np.uint8),
            "bid_px": np.array([100 * PX, int(100.5 * PX), 99 * PX], dtype=np.int64),
            "ask_px": np.array([int(100.6 * PX), int(100.7 * PX), int(99.2 * PX)], dtype=np.int64),
        }
        p = build_second_path(cols)
        assert len(p) == 4
        assert p.bid_high[0] == 100.5 * PX and p.bid_low[0] == 100 * PX
        # quiet seconds 1001/1002 carry the LAST book of second 1000
        # (bid 100.5 / ask 100.7 — the closing quote, not the extremes)
        assert p.bid_high[1] == p.bid_high[2] == 100.5 * PX
        assert p.ask_low[1] == p.ask_high[1] == int(100.7 * PX)
        assert p.bid_high[3] == 99 * PX


class TestLabelMath:
    def entry(self):
        # entry second: bid 100.0 / ask 100.1
        return (100.0, 100.1)

    def test_upside_adverse_and_times_known(self):
        # bid path after entry: 100.0 .. rises to 100.9 at +3s, dips to
        # 99.8 at +1s; window h=30 padded flat afterwards
        quotes = [self.entry()]
        quotes += [(99.8, 99.9), (100.4, 100.5), (100.9, 101.0)]
        quotes += [(100.2, 100.3)] * 32
        p = path_from_quotes(quotes)
        le = LabelEngine(CFG)
        out = le.label_sample(p, 1000 * NS, {30: 0.5, 120: math.nan, 600: math.nan})
        assert out["h30_upside_pts"] == pytest.approx(100.9 - 100.1)
        assert out["h30_adverse_long_pts"] == pytest.approx(100.1 - 99.8)
        assert out["h30_downside_pts"] == pytest.approx(100.0 - 99.9)
        assert out["h30_adverse_short_pts"] == pytest.approx(101.0 - 100.0)
        assert out["h30_time_to_high_s"] == 3.0
        assert out["h30_time_to_low_s"] == 1.0
        assert out["h30_time_to_high_frac"] == pytest.approx(0.1)
        # dual units (v2.1): pts / sigma
        assert out["h30_upside_norm"] == pytest.approx(0.8 / 0.5)
        assert out["h30_cost_norm"] == pytest.approx(0.2 / 0.5)
        assert out["h30_window_complete"] == 1.0

    def test_direction_bullish_known(self):
        # upside 0.9 pts, adverse 0.1 (spread only): fav = 0.7,
        # net_adv = 0.3, ratio 2 -> 0.7 >= 0.6 -> BULLISH
        quotes = [self.entry()] + [(101.0, 101.1)] * 35
        p = path_from_quotes(quotes)
        out = LabelEngine(CFG).label_sample(p, 1000 * NS, {30: 1.0, 120: 1.0, 600: 1.0})
        assert out["h30_direction"] == DIR_BULLISH

    def test_direction_bearish_mirror(self):
        quotes = [self.entry()] + [(99.0, 99.1)] * 35
        p = path_from_quotes(quotes)
        out = LabelEngine(CFG).label_sample(p, 1000 * NS, {30: 1.0, 120: 1.0, 600: 1.0})
        assert out["h30_direction"] == DIR_BEARISH

    def test_direction_no_trade_when_cost_eats_the_move(self):
        # 3-tick favorable move: fav = 0.3-0.2 = 0.1 < 2 x (0.1+0.2)
        quotes = [self.entry()] + [(100.4, 100.5)] * 35
        p = path_from_quotes(quotes)
        out = LabelEngine(CFG).label_sample(p, 1000 * NS, {30: 1.0, 120: 1.0, 600: 1.0})
        assert out["h30_direction"] == DIR_NO_TRADE

    def test_incomplete_window_flagged_nan(self):
        quotes = [self.entry()] + [(100.2, 100.3)] * 10  # < 30 s of future
        p = path_from_quotes(quotes)
        out = LabelEngine(CFG).label_sample(p, 1000 * NS, {30: 1.0, 120: 1.0, 600: 1.0})
        assert out["h30_window_complete"] == 0.0
        assert math.isnan(out["h30_upside_pts"])

    def test_final_return_mid_based(self):
        quotes = [self.entry()] + [(100.5, 100.6)] * 35
        p = path_from_quotes(quotes)
        out = LabelEngine(CFG).label_sample(p, 1000 * NS, {30: 2.0, 120: 1.0, 600: 1.0})
        assert out["h30_final_return_pts"] == pytest.approx(0.5)
        assert out["h30_final_return_norm"] == pytest.approx(0.25)

    def test_metadata_records_convention(self):
        m = LabelEngine(CFG).metadata()
        assert m["convention"] == "touch_opposite"
        assert m["cost_round_trip_pts"] == pytest.approx(0.2)
        assert m["direction_ratio"] == 2.0


class TestUniquenessWeights:
    def test_isolated_windows_weight_one(self):
        w = uniqueness_weights(np.array([0, 100]), 30, (0, 100))
        assert np.allclose(w, 1.0)

    def test_overlapping_windows_share(self):
        w = uniqueness_weights(np.array([0, 1]), 2, (0, 1))
        # windows cover seconds {1,2} and {2,3}: concurrency 1,2,1
        assert np.allclose(w, 0.75)

    def test_dense_clock_sampling_weights_near_1_over_h(self):
        starts = np.arange(0, 200)
        w = uniqueness_weights(starts, 30, (0, 199))
        mid = w[50:150]
        assert np.allclose(mid, 1.0 / 30, atol=0.01)
        assert w.sum() < len(starts) / 20  # effective N << row count


class TestSampleSelection:
    def build(self, calendar=None, seconds=400):
        from test_normalization_synthetic import steady_session

        cols = steady_session(mult=1, seconds=seconds)
        cal = calendar if calendar is not None else EventCalendar()
        return build_session_samples(
            dt.date(2024, 1, 16), CFG, cols=cols, calendar=cal
        ), cols

    def test_clock_cadence_and_spacing(self):
        (df, summary), cols = self.build()
        assert summary["by_trigger"].get("clock", 0) > 100
        gaps = np.diff(df["ts"].to_numpy()) / NS
        assert (gaps >= 0.5 - 1e-9).all()  # minimum-spacing rule

    def test_warmup_produces_no_samples(self):
        (df, _), cols = self.build()
        t0 = int(cols["ts"][0])
        # nothing sampled before the scales warmed (~150 s in)
        assert (df["ts"] - t0).min() / NS > 100

    def test_release_exclusion_inside_window(self):
        from test_normalization_synthetic import steady_session

        cols = steady_session(mult=1, seconds=400)
        t_mid = int(cols["ts"][len(cols["ts"]) // 2])
        cal = EventCalendar()
        cal.events.append(ScheduledEvent(t_mid, "CPI", "high", "fixture"))
        (df, summary), _ = self.build(calendar=cal)
        assert (df["release_policy"] == "exclude").any()
        excluded = df[df["release_policy"] == "exclude"]
        assert (excluded["h30_trainable"] == 0.0).all()

    def test_trainable_requires_complete_window_and_sigma(self):
        (df, _), _ = self.build()
        ok = df[df["h30_trainable"] == 1.0]
        assert len(ok) > 50
        assert (ok["h30_window_complete"] == 1.0).all()
        assert (ok["h30_sigma_pts"] > 0).all()
        assert ok["h30_upside_norm"].notna().all()
        # tail samples with incomplete windows exist and are not trainable
        tail = df[df["h30_window_complete"] == 0.0]
        assert (tail["h30_trainable"] == 0.0).all()

    def test_effective_n_reported_below_rowcount(self):
        (df, summary), _ = self.build()
        h30 = summary["h30"]
        assert 0 < h30["effective_n"] < h30["trainable"]

    def test_determinism(self):
        (a, sa), _ = self.build()
        from test_normalization_synthetic import steady_session

        cols = steady_session(mult=1, seconds=400)
        b, sb = build_session_samples(
            dt.date(2024, 1, 16), CFG, cols=cols, calendar=EventCalendar()
        )
        assert a.equals(b)
        assert sa["by_trigger"] == sb["by_trigger"]
