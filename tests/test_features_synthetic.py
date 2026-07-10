"""Known-answer unit tests for the Phase 3 feature engine (Steps 7-12).

Every scenario is pushed through the REAL compiled path — gc_core engine
with lifecycle + flow recording — then the drained primitive columns are
streamed through FeatureEngine.step(), so these tests cover the Rust
primitives and the Python composition together. Answers are known by
construction.

Composite-score scale invariance (plan Phase 3 addition): identical relative
behavior at 3x volume must produce identical bounded scores.
"""
import datetime as dt
import sys
from pathlib import Path

import gc_core
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from features.core_features import FeatureEngine  # noqa: E402
from utilities.config import load_config  # noqa: E402

CFG = load_config()
PX = 1_000_000_000  # 1.0 point
TICK = PX // 10  # GC tick = 0.1 pt
LAST = gc_core.F_LAST
S = 1_000_000_000  # 1 s in ns
IID = 1


def engine():
    e = gc_core.MboEngine(lifecycle=True)
    e.enable_flow(IID, TICK, CFG.features.near_touch_ticks, CFG.features.book_levels)
    return e


def drain(e) -> dict[str, np.ndarray]:
    e.finish()
    return {n: np.frombuffer(b, dtype=np.dtype(d)) for n, d, b in e.flow_drain()}


def book(e, ts=1_000, bid=100.0, ask=100.1, sz=10):
    """Seed a simple two-sided book (order ids 9001/9002)."""
    e.push(ts, "A", "B", int(bid * PX), sz, 9001)
    e.push(ts, "A", "A", int(ask * PX), sz, 9002, LAST)


def feats(cols) -> list[dict]:
    fe = FeatureEngine(CFG)
    _, kept = fe.run(cols, sample_every_ns=0)
    return kept


class TestAggressiveDelta:  # Step 7
    def test_t_only_delta_and_ratio(self):
        e = engine()
        book(e)
        e.push(2 * S, "T", "B", 100 * PX + TICK, 10, 0)
        e.push(2 * S, "T", "A", 100 * PX, 4, 0, LAST)
        out = feats(drain(e))[-1]
        assert out["aggr_delta_s"] == 6
        assert out["aggr_delta_ratio_s"] == pytest.approx(6 / 14)

    def test_fills_never_count_as_volume(self):
        # the T-only rule (Phase 1): F records must not move delta
        e = engine()
        book(e)
        e.push(2 * S, "T", "B", int(100.1 * PX), 4, 0)
        e.push(2 * S, "F", "A", int(100.1 * PX), 4, 9002)
        e.push(2 * S, "M", "A", int(100.1 * PX), 6, 9002, LAST)
        out = feats(drain(e))[-1]
        assert out["aggr_delta_s"] == 4  # from T alone, not T+F

    def test_window_expiry(self):
        e = engine()
        book(e)
        e.push(2 * S, "T", "B", int(100.1 * PX), 10, 0, LAST)
        e.push(30 * S, "A", "B", 99 * PX, 1, 7, LAST)  # quiet group later
        rows = feats(drain(e))
        assert rows[-2]["aggr_delta_s"] == 10
        assert rows[-1]["aggr_delta_s"] == 0  # 2s window long gone
        assert rows[-1]["aggr_delta_l"] == 10  # 60s window still holds it
        assert rows[-1]["aggr_delta_ratio_l"] == 1.0


class TestReplenishment:  # Step 8
    def test_adds_vs_removals_at_best(self):
        e = engine()
        book(e, sz=10)
        # at best bid: add 10 more, pull 5, fill 5 (T+F+M)
        e.push(2 * S, "A", "B", 100 * PX, 10, 11, LAST)
        e.push(3 * S, "A", "B", 100 * PX, 5, 12, LAST)
        e.push(4 * S, "C", "B", 100 * PX, 5, 12, LAST)  # pull 5
        e.push(5 * S, "T", "A", 100 * PX, 5, 0)
        e.push(5 * S, "F", "B", 100 * PX, 5, 9001)
        e.push(5 * S, "M", "B", 100 * PX, 5, 9001, LAST)  # fill 5 applied
        out = feats(drain(e))[-1]
        # adds at best: 10 (seed) + 10 + 5 = 25; pulls 5 + fills 5 -> 25/35
        assert out["replenish_bid_m"] == pytest.approx(25 / 35)

    def test_neutral_after_windows_drain(self):
        e = engine()
        book(e)
        # 30s later, far from the touch: all best-flow windows are empty
        e.push(30 * S, "A", "B", 90 * PX, 1, 7, LAST)
        out = feats(drain(e))[-1]
        assert out["replenish_bid_m"] == 0.5
        assert out["replenish_ask_m"] == 0.5


class TestPriceProgress:  # Step 9
    def test_signed_progress_and_efficiency(self):
        e = engine()
        book(e)  # mid 100.05
        # move at 12s: both the short and mid windows are past warm-up
        # (the boundary value at t-W exists), then lift mid by 1 tick
        e.push(12 * S, "A", "B", 100 * PX + 2 * TICK, 5, 21, LAST)
        rows = feats(drain(e))
        assert rows[-1]["price_progress_ticks_s"] == pytest.approx(1.0)
        assert rows[-1]["price_progress_ticks_m"] == pytest.approx(1.0)
        assert rows[-1]["directional_efficiency_m"] == pytest.approx(1.0)

    def test_round_trip_kills_efficiency(self):
        e = engine()
        book(e)
        e.push(2 * S, "A", "B", 100 * PX + 2 * TICK, 5, 21, LAST)  # mid +1
        e.push(3 * S, "C", "B", 100 * PX + 2 * TICK, 5, 21, LAST)  # mid -1
        out = feats(drain(e))[-1]
        assert out["price_progress_ticks_m"] == pytest.approx(0.0)
        assert out["directional_efficiency_m"] == pytest.approx(0.0)


class TestAbsorption:  # Steps 10-11 (numeric; visual = notebook)
    def scenario(self, mult: int):
        """Heavy selling into a static, replenishing bid. mult scales ALL
        volumes (scale-invariance check)."""
        e = engine()
        book(e, sz=30 * mult)
        # 60s of baseline selling so the long window has the same rate
        e.push(2 * S, "T", "A", 100 * PX, 30 * mult, 0)
        e.push(2 * S, "F", "B", 100 * PX, 30 * mult, 9001)
        e.push(2 * S, "C", "B", 100 * PX, 30 * mult, 9001, LAST)
        # bid replenishes at the same price instantly (same displayed size)
        e.push(2 * S + 1_000, "A", "B", 100 * PX, 30 * mult, 31, LAST)
        return feats(drain(e))[-1]

    def test_one_sided_absorption_value(self):
        out = self.scenario(1)
        # burst = 30/(30 + 30*(2/60)) = 30/31; stall = 1 (mid static);
        # hold = adds 30 / (adds 30 + fills 30 + pulls 0) = 0.5
        assert out["absorption_bid_s"] == pytest.approx(30 / 31 * 0.5, rel=1e-6)
        assert out["absorption_ask_s"] == 0.0
        assert out["absorption_net_s"] > 0  # bid side absorbing = supportive

    def test_scale_invariance_3x_volume(self):
        a, b = self.scenario(1), self.scenario(3)
        for k in ("absorption_bid_s", "absorption_ask_s", "absorption_net_s",
                  "replenish_bid_m", "aggr_delta_ratio_s", "book_imbalance_l1"):
            assert a[k] == pytest.approx(b[k], abs=1e-12), k

    def test_price_moving_kills_absorption(self):
        e = engine()
        book(e, sz=30)
        e.push(2 * S, "T", "A", 100 * PX, 30, 0)
        e.push(2 * S, "F", "B", 100 * PX, 30, 9001)
        e.push(2 * S, "C", "B", 100 * PX, 30, 9001, LAST)
        # bid does NOT replenish; next bid is 5 ticks lower -> mid falls
        e.push(2 * S + 1_000, "A", "B", 100 * PX - 5 * TICK, 30, 31, LAST)
        out = feats(drain(e))[-1]
        moving = out["absorption_bid_s"]
        holding = self.scenario(1)["absorption_bid_s"]
        assert moving < holding * 0.5  # stall + hold both collapse


class TestPullingStacking:
    def test_net_near_touch_flow(self):
        e = engine()
        book(e)
        e.push(2 * S, "A", "B", 100 * PX - TICK, 20, 41, LAST)  # stack near
        e.push(3 * S, "A", "B", 100 * PX - 2 * TICK, 10, 42, LAST)
        e.push(4 * S, "C", "B", 100 * PX - 2 * TICK, 10, 42, LAST)  # pull it
        out = feats(drain(e))[-1]
        # near-touch bid adds: 10 (seed) + 20 + 10 = 40, pulls 10 -> 30/50
        assert out["stacking_bid_m"] == pytest.approx(0.6)
        assert out["stacking_ask_m"] == pytest.approx(1.0)  # seed ask add only
        assert out["stacking_net_m"] == pytest.approx(-0.4)


class TestMlofiAndImbalance:
    def test_level1_add_and_pull(self):
        e = engine()
        book(e, sz=10)
        e.push(2 * S, "A", "B", 100 * PX, 5, 51, LAST)  # best bid 10 -> 15
        rows = feats(drain(e))
        assert rows[-1]["mlofi_1_s"] == 5
        assert rows[-1]["mlofi_near_s"] == 5

    def test_book_imbalance_known(self):
        e = engine()
        e.push(1_000, "A", "B", 100 * PX, 60, 1)
        e.push(1_000, "A", "A", int(100.1 * PX), 20, 2, LAST)
        out = feats(drain(e))[-1]
        assert out["book_imbalance_l1"] == pytest.approx(0.5)
        assert out["book_imbalance_near"] == pytest.approx(0.5)

    def test_microprice_sign(self):
        e = engine()
        e.push(1_000, "A", "B", 100 * PX, 10, 1)
        e.push(1_000, "A", "A", int(100.1 * PX), 30, 2, LAST)
        out = feats(drain(e))[-1]
        # heavy ask, light bid -> micro pulled toward the bid -> negative
        micro = (100 * PX * 30 + int(100.1 * PX) * 10) / 40 / TICK
        mid = (100 * PX + int(100.1 * PX)) / 2 / TICK
        assert out["microprice_disp_ticks"] == pytest.approx(micro - mid)
        assert out["microprice_disp_ticks"] < 0


class TestSweeps:
    def sweep_session(self, reclaim: bool):
        e = engine()
        e.push(1_000, "A", "B", 100 * PX, 10, 1)
        e.push(1_000, "A", "A", int(100.1 * PX), 5, 2)
        e.push(1_000, "A", "A", int(100.2 * PX), 5, 3)
        e.push(1_000, "A", "A", int(100.3 * PX), 5, 4, LAST)
        # buy sweep through two ask levels in ONE matching event
        e.push(2 * S, "T", "B", int(100.1 * PX), 5, 0)
        e.push(2 * S, "F", "A", int(100.1 * PX), 5, 2)
        e.push(2 * S, "C", "A", int(100.1 * PX), 5, 2)
        e.push(2 * S, "T", "B", int(100.2 * PX), 5, 0)
        e.push(2 * S, "F", "A", int(100.2 * PX), 5, 3)
        e.push(2 * S, "C", "A", int(100.2 * PX), 5, 3, LAST)
        if reclaim:
            # asks re-stack at the swept prices -> mid falls back
            e.push(3 * S, "A", "A", int(100.1 * PX), 5, 61, LAST)
        # a row after the reclaim horizon closes the sweep bookkeeping
        e.push(20 * S, "A", "B", 90 * PX, 1, 62, LAST)
        return feats(drain(e))

    def test_sweep_detected_and_measured(self):
        rows = self.sweep_session(reclaim=False)
        swept = [r for r in rows if r["sweep_buy_ticks_m"] > 0]
        assert swept and swept[0]["sweep_buy_ticks_m"] == pytest.approx(1.0)
        assert all(r["sweep_sell_ticks_m"] == 0 for r in rows)

    def test_reclaim_scores_failure(self):
        rows = self.sweep_session(reclaim=True)
        assert max(r["sweep_failure_score"] for r in rows) > 0.5
        assert rows[-1]["failed_sweeps_l"] == 1.0

    def test_holding_sweep_not_failed(self):
        rows = self.sweep_session(reclaim=False)
        assert rows[-1]["failed_sweeps_l"] == 0.0


class TestQueueAndSurvival:
    def test_queue_depletion_known(self):
        e = engine()
        book(e, sz=25)
        # 20 lots execute at the bid; 5 remain displayed
        e.push(2 * S, "T", "A", 100 * PX, 20, 0)
        e.push(2 * S, "F", "B", 100 * PX, 20, 9001)
        e.push(2 * S, "M", "B", 100 * PX, 5, 9001, LAST)
        out = feats(drain(e))[-1]
        assert out["queue_depletion_bid_s"] == pytest.approx(20 / 25)

    def test_survival_and_cancel_before_touch(self):
        e = engine()
        book(e, sz=5)
        # three bids: one fills (touched+survived), one pulled after touch,
        # one pulled untouched (far from market)
        e.push(2 * S, "A", "B", 100 * PX, 5, 71)      # at best (touched)
        e.push(2 * S, "A", "B", 90 * PX, 5, 72, LAST)  # far
        e.push(3 * S, "T", "A", 100 * PX, 5, 0)
        e.push(3 * S, "F", "B", 100 * PX, 5, 9001)
        e.push(3 * S, "C", "B", 100 * PX, 5, 9001, LAST)  # filled termination
        # pull the far order FIRST (while better bids still shield it from
        # ever becoming best), then the touched one
        e.push(4 * S, "C", "B", 90 * PX, 5, 72, LAST)   # pulled, untouched
        e.push(5 * S, "C", "B", 100 * PX, 5, 71, LAST)  # pulled, was touched
        out = feats(drain(e))[-1]
        assert out["liquidity_survival_ratio_l"] == pytest.approx(1 / 2)
        assert out["cancel_before_touch_rate_l"] == pytest.approx(1 / 2)

    def test_queue_turnover_known(self):
        e = engine()
        book(e, sz=5)
        e.push(2 * S, "A", "B", 100 * PX, 10, 81, LAST)
        e.push(3 * S, "C", "B", 100 * PX, 10, 81, LAST)
        out = feats(drain(e))[-1]
        # churn at best bid = 5 (seed) + 10 add + 10 pull = 25; standing 5
        assert out["queue_turnover_bid_m"] == pytest.approx(25 / 30)


class TestIcebergAndLifetime:
    def test_refill_chain_raises_iceberg_score(self):
        e = engine()
        book(e, sz=5)
        # exhaust the displayed bid clip, instant same-size refill (Phase 2
        # chain link, confidence 1.0), then no other flow
        e.push(2 * S, "T", "A", 100 * PX, 5, 0)
        e.push(2 * S, "F", "B", 100 * PX, 5, 9001)
        e.push(2 * S, "C", "B", 100 * PX, 5, 9001, LAST)
        e.push(2 * S, "A", "B", 100 * PX, 5, 91, LAST)  # dt=0 -> conf 1.0
        out = feats(drain(e))[-1]
        # freq = 1/(1+4) = 0.2, conf_mean = 1.0, hidden 0 -> score 0.2
        assert out["iceberg_score_bid_l"] == pytest.approx(0.2)
        assert out["iceberg_score_ask_l"] == 0.0

    def test_hidden_volume_raises_iceberg_score(self):
        e = engine()
        book(e, sz=2)
        # fill 10 against a displayed 2 -> hidden 8
        e.push(2 * S, "T", "A", 100 * PX, 10, 0)
        e.push(2 * S, "F", "B", 100 * PX, 10, 9001)
        e.push(2 * S, "C", "B", 100 * PX, 2, 9001, LAST)
        out = feats(drain(e))[-1]
        assert out["iceberg_score_bid_l"] == pytest.approx(8 / 18)

    def test_lifetime_mean_and_chain_adjustment(self):
        e = engine()
        book(e)
        e.push(2 * S, "A", "B", 99 * PX, 5, 95, LAST)
        e.push(3 * S, "C", "B", 99 * PX, 5, 95, LAST)  # lifetime 1s
        e.push(3 * S + 1000, "A", "B", 98 * PX, 5, 96, LAST)
        e.push(6 * S + 1000, "C", "B", 98 * PX, 5, 96, LAST)  # lifetime 3s
        out = feats(drain(e))[-1]
        assert out["order_lifetime_ms_l"] == pytest.approx(2000.0)
        assert out["order_lifetime_chain_adj_ms_l"] == pytest.approx(2000.0)


class TestImpactBurstAge:
    def test_price_impact_known(self):
        e = engine()
        book(e)  # bid 100@10, ask 100.1@10
        e.push(1_000, "A", "A", int(100.2 * PX), 10, 9003, LAST)
        # at 12s (windows warm): 10 lots lift the whole best ask level
        e.push(12 * S, "T", "B", int(100.1 * PX), 10, 0)
        e.push(12 * S, "F", "A", int(100.1 * PX), 10, 9002)
        e.push(12 * S, "C", "A", int(100.1 * PX), 10, 9002, LAST)
        out = feats(drain(e))[-1]
        # mid 100.05 -> 100.10 = +0.5 tick on 10 lots
        assert out["price_progress_ticks_m"] == pytest.approx(0.5)
        assert out["price_impact_m"] == pytest.approx(0.5 / 10)

    def test_trade_burst_intensity(self):
        # a lone trade after silence: n_s=1, n_l=1 -> 1/(1+1*(2/60)) = 30/31
        e = engine()
        book(e)
        e.push(30 * S, "T", "B", int(100.1 * PX), 1, 0, LAST)
        out = feats(drain(e))[-1]
        assert out["trade_burst_intensity_s"] == pytest.approx(30 / 31)

        # steady 1 trade per 2s for 60s -> short window holds exactly the
        # long-window average -> 0.5
        e2 = engine()
        book(e2)
        for k in range(30):
            e2.push((2 + 2 * k) * S, "T", "B", int(100.1 * PX), 1, 0, LAST)
        out2 = feats(drain(e2))[-1]
        assert out2["trade_burst_intensity_s"] == pytest.approx(0.5, abs=0.02)

    def test_liquidity_age_weighted(self):
        e = engine()
        book(e)  # both best orders born at ts=1000
        e.push(10 * S, "A", "B", 90 * PX, 1, 99, LAST)  # far; just a row
        rows = feats(drain(e))
        assert rows[-1]["liquidity_age_bid_s"] == pytest.approx(10.0, abs=0.01)
        assert rows[-1]["liquidity_age_imbalance"] == pytest.approx(0.0)
        # equal size joins the best bid at 20s -> size-weighted mean halves
        e2 = engine()
        book(e2)
        e2.push(20 * S, "A", "B", 100 * PX, 10, 98, LAST)
        out = feats(drain(e2))[-1]
        assert out["liquidity_age_bid_s"] == pytest.approx(10.0, abs=0.01)
        assert out["liquidity_age_ask_s"] == pytest.approx(20.0, abs=0.01)
        assert out["liquidity_age_imbalance"] == pytest.approx(-1 / 3, abs=0.01)


class TestDepthShape:
    def test_vacuum_rises_when_ask_depth_vanishes(self):
        e = engine()
        # establish depth baseline over many groups, then pull the asks
        e.push(1_000, "A", "B", 100 * PX, 10, 1)
        e.push(1_000, "A", "A", int(100.1 * PX), 20, 2)
        e.push(1_000, "A", "A", int(100.2 * PX), 20, 3, LAST)
        for k in range(10):
            e.push((2 + k) * S, "A", "B", 99 * PX - k * TICK, 1, 100 + k, LAST)
        e.push(15 * S, "C", "A", int(100.1 * PX), 20, 2, LAST)
        e.push(16 * S, "C", "A", int(100.2 * PX), 20, 3, LAST)
        rows = feats(drain(e))
        assert rows[-1]["liquidity_vacuum_up"] > 0.8
        assert rows[5]["liquidity_vacuum_up"] == pytest.approx(0.5, abs=0.05)

    def test_resiliency_trough_and_recovery(self):
        e = engine()
        book(e, sz=20)
        for k in range(10):  # baseline rows
            e.push((2 + k) * S, "A", "A", 200 * PX + k * TICK, 1, 100 + k, LAST)
        e.push(13 * S, "C", "B", 100 * PX, 20, 9001, LAST)  # bid depth -> 0
        e.push(14 * S, "A", "B", 100 * PX, 20, 200, LAST)   # full recovery
        rows = feats(drain(e))
        assert rows[-2]["book_resiliency_bid"] == pytest.approx(0.0)
        assert rows[-1]["book_resiliency_bid"] == pytest.approx(1.0)


class TestOneCodePath:
    def test_step_equals_run_row_by_row(self):
        """Live-style length-1 columns through step() must equal the
        historical run() over the same drained buffer (Critical Rule 3)."""
        e = engine()
        book(e)
        e.push(2 * S, "T", "B", int(100.1 * PX), 4, 0)
        e.push(2 * S, "F", "A", int(100.1 * PX), 4, 9002)
        e.push(2 * S, "M", "A", int(100.1 * PX), 6, 9002, LAST)
        e.push(3 * S, "A", "B", 100 * PX - TICK, 7, 55, LAST)
        cols = drain(e)

        _, hist = FeatureEngine(CFG).run(cols, sample_every_ns=0)
        live_fe = FeatureEngine(CFG)
        live = [
            live_fe.step({k: v[i : i + 1] for k, v in cols.items()}, 0)
            for i in range(len(cols["ts"]))
        ]
        assert hist == live

    def test_determinism_two_runs(self):
        e = engine()
        book(e)
        e.push(2 * S, "T", "B", int(100.1 * PX), 4, 0, LAST)
        cols = drain(e)
        a = FeatureEngine(CFG).run(cols, 0)[1]
        b = FeatureEngine(CFG).run(cols, 0)[1]
        assert a == b
