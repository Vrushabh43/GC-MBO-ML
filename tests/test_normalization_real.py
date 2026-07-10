"""Real-data verification of Phase 4/4A on a dev-slice session: scale
plausibility, exact twin arithmetic against the emitted scales, the
do-not-normalize passthrough, warm-up behavior, percentile bounds, and
determinism. (Session 2026-01-04 is a single Sunday session — no intra-file
maintenance crossing; the reset path is covered synthetically.)
"""
import datetime as dt
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from features.core_features import FeatureEngine  # noqa: E402
from features.flow_stream import replay_session_flow  # noqa: E402
from features.normalization import NormalizedFeatureEngine  # noqa: E402
from utilities.config import load_config  # noqa: E402

CFG = load_config()
SESSION = dt.date(2026, 1, 4)


@pytest.fixture(scope="module")
def session():
    fs = replay_session_flow(SESSION, CFG)
    nfe = NormalizedFeatureEngine(CFG)
    names, rows = nfe.run(fs.cols, sample_every_ns=1_000_000_000)
    return fs, names, rows


class TestScalesOnRealData:
    def test_warmup_then_ready_for_rest_of_session(self, session):
        """The Sunday file contains the 17:00-18:00 ET pre-open hour, so
        state warms, RESETS at the 18:00 ET session boundary (by design),
        then re-warms ~150s later and stays ready to the end."""
        _, _, rows = session
        ready = [r["norm_ready"] for r in rows]
        assert ready[0] == 0.0
        last_cold = max(i for i, v in enumerate(ready) if v == 0.0)
        assert last_cold < len(rows) // 4  # re-warmed early in the session
        assert all(v == 1.0 for v in ready[last_cold + 1 :])
        assert sum(ready) / len(ready) > 0.9

    def test_scales_plausible_for_gc(self, session):
        _, _, rows = session
        warm = [r for r in rows if r["norm_ready"] == 1.0]
        v = [r["v_scale"] for r in warm]
        d = [r["d_scale"] for r in warm]
        s = [r["sigma_dist"] for r in warm]
        assert all(x >= 0 for x in v) and max(v) < 1000  # contracts/second
        assert all(x > 0 for x in d) and max(d) < 100_000  # contracts
        assert all(x >= 0 for x in s) and max(s) < 50  # points per 2 minutes
        assert max(s) > 0  # a real session moves

    def test_sigma_horizons_ordered(self, session):
        """Longer horizons accumulate larger median moves (weakly)."""
        _, _, rows = session
        last = rows[-1]
        assert last["sigma_30s"] <= last["sigma_120s"] <= last["sigma_600s"]

    def test_twin_arithmetic_exact(self, session):
        _, _, rows = session
        tick_pts = 0.1
        checked = 0
        for r in rows:
            if r["norm_ready"] != 1.0 or r["sigma_dist"] <= 0:
                continue
            assert r["price_progress_ticks_m_norm"] == pytest.approx(
                r["price_progress_ticks_m"] * tick_pts / r["sigma_dist"]
            )
            assert r["aggr_delta_m_norm"] == pytest.approx(
                r["aggr_delta_m"] / (r["v_scale"] * CFG.features.window_mid_s)
            ) or r["v_scale"] == 0
            assert r["mlofi_near_m_norm"] == pytest.approx(
                r["mlofi_near_m"] / r["d_scale"]
            )
            checked += 1
        assert checked > 1000

    def test_percentiles_bounded(self, session):
        _, _, rows = session
        for r in rows:
            for k in ("absorption_net_s_pctile", "spread_ticks_pctile",
                      "trade_burst_intensity_s_pctile",
                      "sigma_dist_pct", "v_scale_pct", "d_scale_pct"):
                if not math.isnan(r[k]):
                    assert 0.0 <= r[k] <= 1.0, k


class TestDisciplinesOnRealData:
    def test_do_not_normalize_passthrough(self, session):
        """Every Phase 3 raw output is unchanged by the wrapper except the
        absorption trio (sigma-normalized stall ingredient, plan 4A rule 3)."""
        fs, _, rows = session
        _, plain = FeatureEngine(CFG).run(fs.cols, sample_every_ns=1_000_000_000)
        may_differ = {"absorption_bid_s", "absorption_ask_s", "absorption_net_s"}
        assert len(plain) == len(rows)
        diffs = 0
        for p, w in zip(plain, rows, strict=True):
            for k, v in p.items():
                if k in may_differ:
                    diffs += w[k] != v
                    continue
                assert w[k] == v or (math.isnan(v) and math.isnan(w[k])), k
        assert diffs > 0  # the upgrade actually engages on real data

    def test_absorption_still_bounded_after_upgrade(self, session):
        _, _, rows = session
        for r in rows:
            assert 0.0 <= r["absorption_bid_s"] < 1.0
            assert 0.0 <= r["absorption_ask_s"] < 1.0
            assert -1.0 < r["absorption_net_s"] < 1.0

    def test_norm_columns_nan_tracks_per_scale_warmup(self, session):
        """Twins are NaN exactly while THEIR scale is warming (v_scale ~30s,
        d_scale ~30s, sigma_2m ~150s); norm_ready = all three warm."""
        _, _, rows = session
        for r in rows:
            assert math.isnan(r["aggr_delta_m_norm"]) == math.isnan(r["v_scale"])
            assert math.isnan(r["mlofi_near_s_norm"]) == math.isnan(r["d_scale"])
            assert math.isnan(r["price_progress_ticks_m_norm"]) == math.isnan(
                r["sigma_dist"]
            )
            if r["norm_ready"] == 1.0:
                for k in ("aggr_delta_m_norm", "mlofi_near_s_norm",
                          "price_progress_ticks_m_norm"):
                    assert not math.isnan(r[k]), k

    def test_determinism(self, session):
        fs, _, rows = session
        again = NormalizedFeatureEngine(CFG).run(fs.cols, 1_000_000_000)[1]
        for a, b in zip(rows, again, strict=True):
            for k, v in a.items():
                assert b[k] == v or (math.isnan(v) and math.isnan(b[k])), k
