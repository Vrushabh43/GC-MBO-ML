"""Property tests for the Phase 3 feature engine on a real dev-slice
session (plan Phase 3: every feature ships with a property test — bounds,
sign conventions — beside its known-answer unit test).

Also verifies the flow-primitive stream itself against independent engine
counters, and replay-twice determinism of primitives and features.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from features.core_features import FeatureEngine  # noqa: E402
from features.flow_stream import replay_session_flow  # noqa: E402
from mbo_engine.engine import MboEngine  # noqa: E402
from utilities.config import load_config  # noqa: E402

CFG = load_config()
SESSION = dt.date(2026, 1, 4)

# bounded features: name -> (lo, hi)
BOUNDS = {
    "aggr_delta_ratio_s": (-1, 1),
    "aggr_delta_ratio_m": (-1, 1),
    "aggr_delta_ratio_l": (-1, 1),
    "replenish_bid_m": (0, 1),
    "replenish_ask_m": (0, 1),
    "directional_efficiency_m": (0, 1),
    "absorption_bid_s": (0, 1),
    "absorption_ask_s": (0, 1),
    "absorption_net_s": (-1, 1),
    "stacking_bid_m": (-1, 1),
    "stacking_ask_m": (-1, 1),
    "stacking_net_m": (-2, 2),
    "book_imbalance_l1": (-1, 1),
    "book_imbalance_near": (-1, 1),
    "book_imbalance_total": (-1, 1),
    "sweep_failure_score": (0, 1),
    "queue_depletion_bid_s": (0, 1),
    "queue_depletion_ask_s": (0, 1),
    "liquidity_survival_ratio_l": (0, 1),
    "cancel_before_touch_rate_l": (0, 1),
    "iceberg_score_bid_l": (0, 1),
    "iceberg_score_ask_l": (0, 1),
    "liquidity_vacuum_up": (0, 1),
    "liquidity_vacuum_down": (0, 1),
    "book_resiliency_bid": (0, 1),
    "book_resiliency_ask": (0, 1),
    "trade_burst_intensity_s": (0, 1),
    "queue_turnover_bid_m": (0, 1),
    "queue_turnover_ask_m": (0, 1),
}
# NOTE: spread_ticks is deliberately absent — GLBX books legitimately cross
# during pre-open price formation (verified Phase 1 finding #6).
NON_NEGATIVE = [
    "sweep_buy_ticks_m", "sweep_sell_ticks_m", "failed_sweeps_l",
    "price_impact_m", "order_lifetime_ms_l", "order_lifetime_chain_adj_ms_l",
    "liquidity_age_bid_s", "liquidity_age_ask_s",
]


@pytest.fixture(scope="module")
def session():
    fs = replay_session_flow(SESSION, CFG)
    fe = FeatureEngine(CFG)
    names, rows = fe.run(fs.cols, sample_every_ns=1_000_000_000)
    return fs, names, rows


class TestFlowPrimitives:
    def test_trade_volume_reconciles_with_engine(self, session):
        """Σ(t_buy+t_sell) over flow rows == the engine's T volume for the
        tracked instrument (independent counter)."""
        fs, _, _ = session
        e = MboEngine(CFG, lifecycle=False)
        e.replay_date(SESSION)
        engine_vol = dict(
            (iid, vol) for iid, _, _, vol in e.instruments()
        )[fs.instrument_id]
        assert int(fs.cols["t_buy"].sum() + fs.cols["t_sell"].sum()) == engine_vol

    def test_terminations_reconcile_with_lifecycle_records(self, session):
        """Flow termination tallies == Phase 2 lifecycle records for the
        instrument (two independent paths out of the same tracker)."""
        fs, _, _ = session
        e = MboEngine(CFG, lifecycle=True)
        e.replay_date(SESSION)
        lc = e.lifecycle_drain()
        m = lc["instrument_id"] == fs.instrument_id
        filled = int((m & (lc["final_state"] == 0)).sum())
        pulled = int((m & np.isin(lc["final_state"], (1, 2))).sum())
        assert int(fs.cols["term_filled"].sum()) == filled
        got_pulled = int(
            fs.cols["term_pulled_touched"].sum()
            + fs.cols["term_pulled_untouched"].sum()
        )
        assert got_pulled == pulled
        links = int((m & (lc["chain_index"] > 0)).sum())
        assert int(fs.cols["refill_b"].sum() + fs.cols["refill_a"].sum()) == links

    def test_book_state_matches_prices(self, session):
        fs, _, _ = session
        v = fs.cols["valid"] == 1
        assert v.mean() > 0.99
        # ask above bid on valid rows (crossed pre-open books excepted)
        ok = fs.cols["ask_px"][v] > fs.cols["bid_px"][v]
        assert ok.mean() > 0.99

    def test_replay_twice_identical_primitives(self):
        a = replay_session_flow(SESSION, CFG)
        b = replay_session_flow(SESSION, CFG)
        assert a.cols.keys() == b.cols.keys()
        for k in a.cols:
            assert np.array_equal(a.cols[k], b.cols[k]), k


class TestFeatureProperties:
    def test_all_finite(self, session):
        _, names, rows = session
        for r in rows:
            for k, v in r.items():
                if k in ("mid_ticks", "spread_ticks"):
                    continue  # NaN allowed only pre-first-valid-book
                assert math.isfinite(v), k

    def test_bounds(self, session):
        _, _, rows = session
        for r in rows:
            for k, (lo, hi) in BOUNDS.items():
                assert lo <= r[k] <= hi, (k, r[k])
            for k in NON_NEGATIVE:
                if math.isfinite(r[k]):
                    assert r[k] >= 0, (k, r[k])

    def test_signed_features_use_both_signs(self, session):
        """Sign conventions carry information: two-sided markets must show
        both signs across a session for the signed features."""
        _, _, rows = session
        for k in ("aggr_delta_ratio_m", "mlofi_near_s", "book_imbalance_l1",
                  "microprice_disp_ticks", "absorption_net_s"):
            vals = [r[k] for r in rows]
            assert min(vals) < 0 < max(vals), k

    def test_features_active_on_real_data(self, session):
        """Every feature must actually move during a real session — a
        constant output means a dead ingredient pipe."""
        _, names, rows = session
        for k in names:
            if k == "ts":
                continue
            vals = {round(r[k], 12) for r in rows}
            assert len(vals) > 1, f"feature never moved: {k}"

    def test_feature_determinism(self, session):
        fs, _, rows = session
        _, again = FeatureEngine(CFG).run(fs.cols, sample_every_ns=1_000_000_000)
        assert rows == again
