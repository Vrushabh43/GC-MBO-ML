"""Known-answer tests for Phase 4/4A normalization.

Covers: the rolling-median/percentile primitives, exact scale values on
constructed streams, warm-up NaN semantics + norm_ready, session-boundary
resets (the intra-file 17:00-18:00 ET maintenance break), exact twin
arithmetic, raw-feature passthrough, one-code-path equality, past-only
(no-leakage) property, determinism — and the plan 4A verification
requirement: ERA INVARIANCE, the same relative event pattern at 1x and 3x
price/volume scale must yield matching _norm features and composite
scores (built end-to-end through the real Rust engine).
"""
import datetime as dt
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import gc_core
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from features.core_features import FeatureEngine  # noqa: E402
from features.normalization import (  # noqa: E402
    NormalizedFeatureEngine,
    RollingMedian,
    RollingPercentile,
)
from utilities.config import load_config  # noqa: E402

CFG = load_config()
PX = 1_000_000_000
TICK = PX // 10
LAST = gc_core.F_LAST
S = 1_000_000_000
IID = 1
# synthetic sessions start mid-session on the ET clock (no boundary): use
# a weekday 05:00 UTC = 00:00 ET (inside the 'asia' phase, far from 17:00)
T0 = int(dt.datetime(2024, 1, 16, 5, 0, tzinfo=dt.timezone.utc).timestamp() * S)


def engine():
    e = gc_core.MboEngine(lifecycle=True)
    e.enable_flow(IID, TICK, CFG.features.near_touch_ticks, CFG.features.book_levels)
    return e


def drain(e):
    e.finish()
    return {n: np.frombuffer(b, dtype=np.dtype(d)) for n, d, b in e.flow_drain()}


class TestPrimitives:
    def test_rolling_median_window(self):
        m = RollingMedian(3)
        for v in (5.0, 1.0, 3.0):
            m.push(v)
        assert m.median() == 3.0
        m.push(9.0)  # evicts 5.0 -> {1,3,9}
        assert m.median() == 3.0
        m.push(9.0)  # evicts 1.0 -> {3,9,9}
        assert m.median() == 9.0

    def test_rolling_percentile_past_only(self):
        p = RollingPercentile(10 * S)
        assert math.isnan(p.percentile(1.0))  # empty history
        for k, v in enumerate((1.0, 2.0, 3.0, 4.0)):
            p.push(k * S, v)
        assert p.percentile(5.0) == 1.0
        assert p.percentile(0.0) == 0.0
        assert p.percentile(2.5) == 0.5
        p.push(20 * S, 10.0)  # evicts every earlier sample (all ts <= 10s)
        assert p.percentile(5.0) == 0.0
        assert p.percentile(11.0) == 1.0


def steady_session(mult: int = 1, seconds: int = 240, wiggle: bool = True):
    """A steady synthetic session through the REAL engine: two-sided book
    (spread 2*mult ticks around `base`), one 2*mult-lot sell per second into
    the best bid with instant same-size replenishment, and (optionally) an
    improving ask flickering with period 7 s — 7 divides neither the 30 s
    nor the 120 s sigma horizon, so h-horizon mid moves are nonzero. Every
    price offset and size scales with `mult` (era scaling: 3x price moves
    AND 3x volumes)."""
    e = engine()
    base = 2000 * mult * PX
    mt = mult * TICK
    t = lambda k, off=0: T0 + k * S + off  # noqa: E731
    e.push(t(0), "A", "B", base - 5 * mt, 30 * mult, 9001)
    e.push(t(0), "A", "B", base - 6 * mt, 30 * mult, 9002)
    e.push(t(0), "A", "A", base + 5 * mt, 30 * mult, 9003)
    e.push(t(0), "A", "A", base + 6 * mt, 30 * mult, 9004, LAST)
    oid = 100
    wig_px = None
    for k in range(1, seconds):
        e.push(t(k), "T", "A", base - 5 * mt, 2 * mult, 0)
        e.push(t(k), "F", "B", base - 5 * mt, 2 * mult, 9001)
        e.push(t(k), "M", "B", base - 5 * mt, 30 * mult - (2 * mult), 9001, LAST)
        e.push(t(k, 1000), "A", "B", base - 5 * mt, 2 * mult, oid, LAST)
        e.push(t(k, 2000), "C", "B", base - 5 * mt, 2 * mult, oid, LAST)
        oid += 1
        if wiggle:
            # the improving ask cycles through 7 distinct levels; mid takes a
            # DISTINCT value on each residue mod 7, so every 30 s and 120 s
            # mid move is nonzero (30 % 7 != 0, 120 % 7 != 0) and the median
            # sigma samples are strictly positive — scaling with mult.
            px = base + (k % 7 - 3) * mt
            if wig_px is not None:
                e.push(t(k, 3000), "C", "A", wig_px, 5 * mult, 99999, LAST)
            e.push(t(k, 4000), "A", "A", px, 5 * mult, 99999, LAST)
            wig_px = px
    return drain(e)


class TestScalesKnownAnswers:
    def test_v_and_d_scales_exact(self):
        # 200s: sigma_2m first samples at t=121s, +30 samples to warm
        cols = steady_session(mult=1, seconds=200, wiggle=False)
        nfe = NormalizedFeatureEngine(CFG)
        _, rows = nfe.run(cols, 0)
        last = rows[-1]
        assert last["norm_ready"] == 1.0
        # 2 lots trade every second -> v_scale = 2.0 contracts/s
        assert last["v_scale"] == pytest.approx(2.0)
        # near+mid bands: bids 30+30, asks 30+30 (transient replenishment
        # order aside) -> median combined depth = 120
        assert last["d_scale"] == pytest.approx(120.0, rel=0.1)
        # static mid -> sigma = 0 -> distance twins divide by the floor;
        # progress is 0 so the twin is exactly 0
        assert last["sigma_dist"] == pytest.approx(0.0)
        assert last["price_progress_ticks_m_norm"] == 0.0

    def test_norm_twin_arithmetic_exact(self):
        cols = steady_session(mult=1, seconds=200)
        nfe = NormalizedFeatureEngine(CFG)
        _, rows = nfe.run(cols, 0)
        r = rows[-1]
        assert r["norm_ready"] == 1.0
        v, d = r["v_scale"], r["d_scale"]
        assert r["aggr_delta_m_norm"] == pytest.approx(
            r["aggr_delta_m"] / (v * CFG.features.window_mid_s)
        )
        assert r["mlofi_near_s_norm"] == pytest.approx(r["mlofi_near_s"] / d)
        if r["sigma_dist"] > 0:
            assert r["microprice_disp_ticks_norm"] == pytest.approx(
                r["microprice_disp_ticks"] * 0.1 / r["sigma_dist"]
            )

    def test_warmup_nan_then_ready(self):
        cols = steady_session(mult=1, seconds=200)
        nfe = NormalizedFeatureEngine(CFG)
        _, rows = nfe.run(cols, 0)
        assert rows[0]["norm_ready"] == 0.0
        assert math.isnan(rows[0]["aggr_delta_m_norm"])
        assert math.isnan(rows[0]["v_scale"])
        ready_at = next(i for i, r in enumerate(rows) if r["norm_ready"] == 1.0)
        assert ready_at > 0
        assert all(
            not math.isnan(r["aggr_delta_m_norm"]) for r in rows[ready_at:]
        )

    def test_sigma_known_move_pattern(self):
        """Best bid steps +1 tick every 2s (ask far and static): mid gains
        0.5 tick per 2s -> the 30s mid move is 7.5 ticks = 0.75 pts."""
        e = engine()
        base = 2000 * PX
        e.push(T0, "A", "A", base + 200 * TICK, 50, 9004, LAST)
        for k in range(180):
            e.push(T0 + k * S, "A", "B", base + (k // 2) * TICK, 10, 100 + k, LAST)
        cols = drain(e)
        nfe = NormalizedFeatureEngine(CFG)
        _, rows = nfe.run(cols, 0)
        assert rows[-1]["sigma_30s"] == pytest.approx(0.75, abs=0.1)


class TestSessionReset:
    def test_maintenance_boundary_resets_scales(self):
        """Rows on both sides of 18:00 ET (= 23:00 UTC in January): the
        second side must start cold (norm_ready back to 0)."""
        e = engine()
        base = 2000 * PX
        t_pre = int(dt.datetime(2024, 1, 16, 20, 0, tzinfo=dt.timezone.utc).timestamp() * S)
        t_post = int(dt.datetime(2024, 1, 16, 23, 30, tzinfo=dt.timezone.utc).timestamp() * S)
        e.push(t_pre, "A", "B", base, 30, 9001)
        e.push(t_pre, "A", "A", base + TICK, 30, 9002, LAST)
        # 200s of activity: enough for sigma_2m (120s horizon + 30 samples)
        for k in range(1, 200):
            e.push(t_pre + k * S, "T", "B", base + TICK, 1, 0, LAST)
        e.push(t_post, "T", "B", base + TICK, 1, 0, LAST)  # after the break
        cols = drain(e)
        nfe = NormalizedFeatureEngine(CFG)
        _, rows = nfe.run(cols, 0)
        assert rows[-2]["norm_ready"] == 1.0  # warm before the break
        assert rows[-1]["norm_ready"] == 0.0  # cold after it
        assert math.isnan(rows[-1]["v_scale"])


class TestEraInvariance:
    """Plan 4A verification: same relative pattern at 1x and 3x
    price/volume scale -> matching _norm features and composite scores."""

    NORM_KEYS = [
        "aggr_delta_s_norm", "aggr_delta_m_norm", "aggr_delta_l_norm",
        "mlofi_1_s_norm", "mlofi_near_s_norm", "mlofi_near_m_norm",
        "price_progress_ticks_m_norm", "microprice_disp_ticks_norm",
    ]
    SCORE_KEYS = [
        "absorption_bid_s", "absorption_ask_s", "absorption_net_s",
        "replenish_bid_m", "replenish_ask_m", "aggr_delta_ratio_m",
        "book_imbalance_l1", "book_imbalance_near", "queue_depletion_bid_s",
        "trade_burst_intensity_s", "directional_efficiency_m",
        "stacking_bid_m", "queue_turnover_bid_m",
    ]

    def rows_at(self, mult: int):
        cols = steady_session(mult=mult, seconds=220)
        nfe = NormalizedFeatureEngine(CFG)
        _, rows = nfe.run(cols, 0)
        assert rows[-1]["norm_ready"] == 1.0
        return rows[-1]

    def test_norm_features_and_scores_invariant_under_3x(self):
        a, b = self.rows_at(1), self.rows_at(3)
        # the scales themselves must scale with the era ...
        assert b["v_scale"] == pytest.approx(3 * a["v_scale"], rel=1e-9)
        assert b["d_scale"] == pytest.approx(3 * a["d_scale"], rel=1e-9)
        # ... while everything the models consume does NOT
        for k in self.NORM_KEYS:
            assert a[k] == pytest.approx(b[k], rel=1e-6, abs=1e-9), k
        for k in self.SCORE_KEYS:
            assert a[k] == pytest.approx(b[k], rel=1e-6, abs=1e-9), k


class TestDisciplines:
    def test_raw_features_pass_through_untouched(self):
        """Do-not-normalize discipline: every Phase 3 output is passed
        through unchanged — except the absorption trio, whose stall
        ingredient intentionally upgrades to sigma units (plan 4A rule 3)."""
        cols = steady_session(mult=1, seconds=200)
        _, plain = FeatureEngine(CFG).run(cols, 0)
        _, wrapped = NormalizedFeatureEngine(CFG).run(cols, 0)
        may_differ = {"absorption_bid_s", "absorption_ask_s", "absorption_net_s"}
        for p, w in zip(plain, wrapped, strict=True):
            for k, v in p.items():
                if k in may_differ:
                    continue
                assert w[k] == v or (math.isnan(v) and math.isnan(w[k])), k

    def test_one_code_path_step_equals_run(self):
        cols = steady_session(mult=1, seconds=90)
        _, hist = NormalizedFeatureEngine(CFG).run(cols, 0)
        live = NormalizedFeatureEngine(CFG)
        stepped = [
            live.step({k: v[i : i + 1] for k, v in cols.items()}, 0)
            for i in range(len(cols["ts"]))
        ]
        assert len(hist) == len(stepped)
        for h, s in zip(hist, stepped, strict=True):
            for k, v in h.items():
                assert s[k] == v or (math.isnan(v) and math.isnan(s[k])), k

    def test_past_only_no_leakage(self):
        """Outputs at row i are identical whether or not later rows exist."""
        cols = steady_session(mult=1, seconds=200)
        n = len(cols["ts"])
        cut = n // 2
        _, full = NormalizedFeatureEngine(CFG).run(cols, 0)
        truncated = {k: v[:cut] for k, v in cols.items()}
        _, part = NormalizedFeatureEngine(CFG).run(truncated, 0)
        for f, p in zip(full[:cut], part, strict=True):
            for k, v in p.items():
                assert f[k] == v or (math.isnan(v) and math.isnan(f[k])), k

    def test_determinism(self):
        cols = steady_session(mult=1, seconds=150)
        a = NormalizedFeatureEngine(CFG).run(cols, 0)[1]
        b = NormalizedFeatureEngine(CFG).run(cols, 0)[1]
        for x, y in zip(a, b, strict=True):
            for k, v in x.items():
                assert y[k] == v or (math.isnan(v) and math.isnan(y[k])), k

    def test_percentiles_bounded_and_present(self):
        cols = steady_session(mult=1, seconds=200)
        _, rows = NormalizedFeatureEngine(CFG).run(cols, 0)
        seen = 0
        for r in rows:
            for k in ("absorption_net_s_pctile", "spread_ticks_pctile",
                      "sigma_dist_pct", "v_scale_pct", "d_scale_pct"):
                v = r[k]
                if not math.isnan(v):
                    assert 0.0 <= v <= 1.0, k
                    seen += 1
        assert seen > 100

    def test_robust_zscore_known_and_past_only(self):
        from features.normalization import RollingRobustZ

        z = RollingRobustZ(60 * S)
        assert math.isnan(z.zscore(5.0))  # no history
        for k, v in enumerate((10.0, 12.0, 8.0, 10.0, 14.0, 6.0, 10.0)):
            z.push(k * S, v)
        # median 10; deviations vs push-time medians {2,3,0,4,4,0}
        # -> MAD 2.5; z(14) = (14-10)/2.5 = 1.6
        assert z.zscore(14.0) == pytest.approx(1.6)
        assert z.zscore(10.0) == pytest.approx(0.0)

    def test_robust_z_feature_emitted(self):
        cols = steady_session(mult=1, seconds=120)
        _, rows = NormalizedFeatureEngine(CFG).run(cols, 0)
        vals = [r["liquidity_age_bid_s_rz"] for r in rows]
        assert any(not math.isnan(v) for v in vals[5:])

    def test_scale_fingerprint_for_model_bundle(self):
        f = NormalizedFeatureEngine(CFG).scale_config_fingerprint()
        assert f["scale_window_s"] == 3600
        assert f["distance_sigma_horizon_s"] in f["sigma_horizons_s"]
        assert "percentiles" in f
