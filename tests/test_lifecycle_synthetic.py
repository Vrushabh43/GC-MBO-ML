"""Synthetic-exchange tests for the Phase 2 order-lifecycle/queue tracker.

Every test feeds a hand-constructed event sequence whose lifecycle records
are known by construction: terminal states, lifetimes, queue positions,
distances from the market, closest-approach ("survival as price approached"),
Globex priority modify accounting, and the iceberg synthetic-parent chain
heuristic (link window, clip bound, confidence, negative cases).

Prices are DBN fixed-point ints (1e-9); the synthetic tick is 1.0 point (PX).
"""
import gc_core
import numpy as np
import pytest

PX = 1_000_000_000  # 1.0 point in fixed-point
LAST = gc_core.F_LAST
SNAP = gc_core.F_SNAPSHOT
MS = 1_000_000  # 1 ms in ns

FILLED = gc_core.STATE_FILLED
PARTIAL = gc_core.STATE_PARTIAL_CANCELLED
CANCELLED = gc_core.STATE_CANCELLED
CLEARED = gc_core.STATE_CLEARED
EOD = gc_core.STATE_END_OF_DATA
REPLACED = gc_core.STATE_REPLACED


def eng(**kw):
    kw.setdefault("lifecycle", True)
    return gc_core.MboEngine(**kw)


def drain(e) -> dict[str, np.ndarray]:
    return {n: np.frombuffer(b, dtype=np.dtype(d)) for n, d, b in e.lifecycle_drain()}


def by_order(e) -> dict[int, dict]:
    cols = drain(e)
    n = len(cols["order_id"])
    return {
        int(cols["order_id"][i]): {k: v[i] for k, v in cols.items()}
        for i in range(n)
    }


class TestTerminalStates:
    def test_pure_pull_is_cancelled_with_lifetime(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)
        e.push(9_000, "C", "B", 100 * PX, 5, 1, flags=LAST)
        e.finish()
        r = by_order(e)[1]
        assert r["final_state"] == CANCELLED
        assert r["ts_added"] == 1_000 and r["ts_terminated"] == 9_000
        assert r["filled_size"] == 0
        assert r["cancelled_size"] == 5  # resting remainder was pulled
        assert r["final_size"] == 5

    def test_full_fill_removal_is_filled_not_cancelled(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)
        e.push(2_000, "T", "A", 100 * PX, 5, 0)
        e.push(2_000, "F", "B", 100 * PX, 5, 1)
        e.push(2_000, "C", "B", 100 * PX, 5, 1, flags=LAST)
        e.finish()
        r = by_order(e)[1]
        assert r["final_state"] == FILLED
        assert r["filled_size"] == 5
        assert r["cancelled_size"] == 0

    def test_partial_fill_then_pull_is_partial_cancelled(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 10, 1, flags=LAST)
        e.push(2_000, "T", "A", 100 * PX, 4, 0)
        e.push(2_000, "F", "B", 100 * PX, 4, 1)
        e.push(2_000, "M", "B", 100 * PX, 6, 1, flags=LAST)  # fill application
        e.push(9_000, "C", "B", 100 * PX, 6, 1, flags=LAST)  # trader pulls rest
        e.finish()
        r = by_order(e)[1]
        assert r["final_state"] == PARTIAL
        assert r["filled_size"] == 4
        assert r["final_size"] == 6
        # the fill-applying M reduction is execution, NOT cancelled quantity;
        # only the pulled remainder counts
        assert r["cancelled_size"] == 6
        assert r["n_size_decreases"] == 1

    def test_clear_emits_cleared_records(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(1_000, "A", "A", 101 * PX, 5, 2, flags=LAST)
        e.push(5_000, "R", "B", 0, 0, 0, flags=LAST)
        e.finish()
        r = by_order(e)
        assert r[1]["final_state"] == CLEARED
        assert r[2]["final_state"] == CLEARED
        assert r[1]["ts_terminated"] == 5_000

    def test_end_of_data_for_resting_orders(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)
        e.push(7_000, "T", "B", 101 * PX, 1, 0, flags=LAST)  # later ts, no book change
        e.finish()
        r = by_order(e)[1]
        assert r["final_state"] == EOD
        assert r["ts_terminated"] == 7_000  # last ts_event seen
        assert r["final_size"] == 5

    def test_finish_is_idempotent(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)
        e.finish()
        e.finish()
        assert e.lifecycle_len() == 1

    def test_duplicate_add_emits_replaced(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)
        e.push(2_000, "A", "B", 99 * PX, 7, 1, flags=LAST)  # same id again
        e.finish()
        cols = drain(e)
        states = sorted(int(s) for s in cols["final_state"])
        assert states == sorted([REPLACED, EOD])
        i = list(cols["final_state"]).index(REPLACED)
        assert cols["ts_terminated"][i] == 2_000

    def test_every_add_terminates_exactly_once(self):
        e = eng()
        for i in range(1, 6):
            e.push(1_000 * i, "A", "B", (100 - i) * PX, i, i, flags=LAST)
        e.push(10_000, "C", "B", 99 * PX, 1, 1, flags=LAST)
        e.push(11_000, "R", "B", 0, 0, 0, flags=LAST)
        e.finish()
        cols = drain(e)
        assert len(cols["order_id"]) == 5
        assert sorted(cols["order_id"]) == [1, 2, 3, 4, 5]


class TestQueuePositions:
    def test_position_and_volume_ahead_at_add(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(2_000, "A", "B", 100 * PX, 7, 2)
        e.push(3_000, "A", "B", 100 * PX, 2, 3, flags=LAST)
        assert e.queue_position(1, 1) == 0
        assert e.queue_position(1, 3) == 2
        assert e.volume_ahead(1, 1) == 0
        assert e.volume_ahead(1, 2) == 5
        assert e.volume_ahead(1, 3) == 12
        e.finish()
        r = by_order(e)
        assert (r[1]["queue_pos_at_add"], r[1]["vol_ahead_at_add"]) == (0, 0)
        assert (r[2]["queue_pos_at_add"], r[2]["vol_ahead_at_add"]) == (1, 5)
        assert (r[3]["queue_pos_at_add"], r[3]["vol_ahead_at_add"]) == (2, 12)

    def test_position_at_termination_reflects_queue_movement(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(2_000, "A", "B", 100 * PX, 7, 2)
        e.push(3_000, "A", "B", 100 * PX, 2, 3, flags=LAST)
        e.push(4_000, "C", "B", 100 * PX, 5, 1, flags=LAST)  # front leaves
        e.push(5_000, "C", "B", 100 * PX, 2, 3, flags=LAST)  # 3 moved up to pos 1
        e.finish()
        r = by_order(e)
        assert r[1]["queue_pos_at_term"] == 0
        assert r[3]["queue_pos_at_add"] == 2
        assert r[3]["queue_pos_at_term"] == 1

    def test_size_decrease_keeps_priority(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(2_000, "A", "B", 100 * PX, 7, 2, flags=LAST)
        e.push(3_000, "M", "B", 100 * PX, 3, 1, flags=LAST)  # 1 shrinks 5->3
        assert e.queue_position(1, 1) == 0  # Globex: decrease keeps priority
        e.push(4_000, "C", "B", 100 * PX, 3, 1, flags=LAST)
        e.finish()
        r = by_order(e)[1]
        assert r["queue_pos_at_term"] == 0
        assert r["n_size_decreases"] == 1
        assert r["cancelled_size"] == 2 + 3  # voluntary reduction + pulled rest

    def test_size_increase_loses_priority(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(2_000, "A", "B", 100 * PX, 7, 2, flags=LAST)
        e.push(3_000, "M", "B", 100 * PX, 9, 1, flags=LAST)  # 1 grows 5->9
        assert e.queue_position(1, 1) == 1  # Globex: increase -> back of queue
        assert e.volume_ahead(1, 1) == 7
        e.push(4_000, "C", "B", 100 * PX, 9, 1, flags=LAST)
        e.finish()
        r = by_order(e)[1]
        assert r["queue_pos_at_term"] == 1
        assert r["n_size_increases"] == 1
        assert r["max_size"] == 9

    def test_price_change_moves_and_requeues(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(2_000, "A", "B", 99 * PX, 7, 2, flags=LAST)
        e.push(3_000, "M", "B", 99 * PX, 5, 1, flags=LAST)  # 1 moves 100->99
        assert e.queue_position(1, 1) == 1  # back of the 99 queue
        e.finish()
        r = by_order(e)[1]
        assert r["n_price_changes"] == 1
        assert r["price_at_add"] == 100 * PX
        assert r["price_final"] == 99 * PX

    def test_level_ages_front_first(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(4_000, "A", "B", 100 * PX, 7, 2, flags=LAST)
        ages = e.level_ages(1, "B", 100 * PX, 10_000)
        assert ages == [(1, 9_000, 5, False), (2, 6_000, 7, False)]


class TestDistancesAndApproach:
    def test_distance_from_same_side_best_and_mid_at_add(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(1_000, "A", "A", 102 * PX, 5, 2, flags=LAST)  # mid = 101
        e.push(2_000, "A", "B", 97 * PX, 3, 3, flags=LAST)  # 3 pts below best bid
        e.push(9_000, "C", "B", 97 * PX, 3, 3, flags=LAST)
        e.finish()
        r = by_order(e)[3]
        assert r["dist_same_at_add"] == 3 * PX
        assert r["dist_mid2_at_add"] == 2 * 4 * PX  # 2 x (mid 101 - 97)
        assert r["dist_same_at_term"] == 3 * PX

    def test_best_order_has_zero_distance_and_touch(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)
        e.push(9_000, "C", "B", 100 * PX, 5, 1, flags=LAST)
        e.finish()
        r = by_order(e)[1]
        assert r["dist_same_at_add"] == 0
        assert r["min_dist_same"] == 0  # it IS the touch

    def test_closest_approach_tracks_best_walking_toward_order(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1)
        e.push(1_000, "A", "B", 99 * PX, 5, 2, flags=LAST)
        e.push(2_000, "A", "B", 95 * PX, 3, 9, flags=LAST)  # subject, 5 below
        e.push(3_000, "C", "B", 100 * PX, 5, 1, flags=LAST)  # best -> 99
        e.push(4_000, "A", "B", 100 * PX, 5, 3, flags=LAST)  # best back to 100
        e.push(9_000, "C", "B", 95 * PX, 3, 9, flags=LAST)
        e.finish()
        r = by_order(e)[9]
        assert r["dist_same_at_add"] == 5 * PX
        assert r["min_dist_same"] == 4 * PX  # best bid got to 99 = 4 pts away
        assert r["dist_same_at_term"] == 5 * PX  # 100 again at the end

    def test_approach_spans_price_change_segments(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)
        e.push(2_000, "A", "B", 96 * PX, 3, 9, flags=LAST)  # 4 pts away
        e.push(3_000, "M", "B", 90 * PX, 3, 9, flags=LAST)  # moves 10 pts away
        e.push(9_000, "C", "B", 90 * PX, 3, 9, flags=LAST)
        e.finish()
        r = by_order(e)[9]
        # closest approach happened in the FIRST segment (4 pts at 96)
        assert r["min_dist_same"] == 4 * PX
        assert r["n_price_changes"] == 1

    def test_ask_side_approach_symmetric(self):
        e = eng()
        e.push(1_000, "A", "A", 100 * PX, 5, 1)
        e.push(1_000, "A", "A", 101 * PX, 5, 2, flags=LAST)
        e.push(2_000, "A", "A", 105 * PX, 3, 9, flags=LAST)  # 5 above best ask
        e.push(3_000, "C", "A", 100 * PX, 5, 1, flags=LAST)  # best ask -> 101
        e.push(9_000, "C", "A", 105 * PX, 3, 9, flags=LAST)
        e.finish()
        r = by_order(e)[9]
        assert r["dist_same_at_add"] == 5 * PX
        assert r["min_dist_same"] == 4 * PX

    def test_missing_side_yields_sentinels(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)  # no asks ever
        e.push(9_000, "C", "B", 100 * PX, 5, 1, flags=LAST)
        e.finish()
        r = by_order(e)[1]
        assert r["dist_mid2_at_add"] == np.iinfo(np.int64).min  # mid undefined
        assert r["dist_same_at_add"] == 0  # own side exists (itself)


class TestIcebergChains:
    def base(self, e):
        e.push(1_000, "A", "B", 100 * PX, 5, 10, flags=LAST)
        e.push(2_000, "T", "A", 100 * PX, 5, 0)
        e.push(2_000, "F", "B", 100 * PX, 5, 10)
        e.push(2_000, "C", "B", 100 * PX, 5, 10, flags=LAST)  # clip exhausted

    def test_refill_within_window_links_chain(self):
        e = eng()
        self.base(e)
        e.push(2_000 + 1 * MS, "A", "B", 100 * PX, 5, 11, flags=LAST)
        e.push(9 * MS, "C", "B", 100 * PX, 5, 11, flags=LAST)
        e.finish()
        r = by_order(e)
        assert r[10]["chain_id"] == 10 and r[10]["chain_index"] == 0
        assert r[11]["chain_id"] == 10 and r[11]["chain_index"] == 1
        assert r[11]["link_dt_ns"] == 1 * MS
        # exact clip at half the window: 1.0 x (1 - 0.5 x 0.5) = 0.75
        assert r[11]["link_confidence"] == pytest.approx(0.75)
        assert e.lifecycle_stats()[1] == 1  # links_made

    def test_chain_of_three_refills(self):
        e = eng()
        oid = 10
        for k in range(3):
            ts = k * 10 * MS
            e.push(ts + 1_000, "A", "B", 100 * PX, 5, oid + k, flags=LAST)
            e.push(ts + 2_000, "T", "A", 100 * PX, 5, 0)
            e.push(ts + 2_000, "F", "B", 100 * PX, 5, oid + k)
            e.push(ts + 2_000, "C", "B", 100 * PX, 5, oid + k, flags=LAST)
        e.finish()
        r = by_order(e)
        # 10ms spacing exceeds the 2ms window -> no links across iterations?
        # No: refills must be within the window; these are separate roots.
        assert all(r[oid + k]["chain_index"] == 0 for k in range(3))

        e2 = eng()
        for k in range(3):
            ts = k * 1 * MS  # 1ms spacing: within window each time
            e2.push(ts + 1_000, "A", "B", 100 * PX, 5, oid + k, flags=LAST)
            e2.push(ts + 2_000, "T", "A", 100 * PX, 5, 0)
            e2.push(ts + 2_000, "F", "B", 100 * PX, 5, oid + k)
            e2.push(ts + 2_000, "C", "B", 100 * PX, 5, oid + k, flags=LAST)
        e2.finish()
        r2 = by_order(e2)
        assert [r2[oid + k]["chain_index"] for k in range(3)] == [0, 1, 2]
        assert all(r2[oid + k]["chain_id"] == 10 for k in range(3))

    def test_no_link_outside_window(self):
        e = eng()
        self.base(e)
        e.push(2_000 + 3 * MS, "A", "B", 100 * PX, 5, 11, flags=LAST)  # too late
        e.finish()
        assert by_order(e)[11]["chain_index"] == 0

    def test_no_link_wrong_price_or_side(self):
        e = eng()
        self.base(e)
        e.push(2_000 + 1 * MS, "A", "B", 99 * PX, 5, 11)  # wrong price
        e.push(2_000 + 1 * MS, "A", "A", 100 * PX, 5, 12, flags=LAST)  # wrong side
        e.finish()
        r = by_order(e)
        assert r[11]["chain_index"] == 0 and r[12]["chain_index"] == 0

    def test_no_link_when_size_exceeds_clip(self):
        e = eng()
        self.base(e)
        e.push(2_000 + 1 * MS, "A", "B", 100 * PX, 6, 11, flags=LAST)  # > clip 5
        e.finish()
        assert by_order(e)[11]["chain_index"] == 0

    def test_pulled_order_opens_no_refill_slot(self):
        e = eng()
        e.push(1_000, "A", "B", 100 * PX, 5, 10, flags=LAST)
        e.push(2_000, "C", "B", 100 * PX, 5, 10, flags=LAST)  # trader pull
        e.push(2_000 + 1 * MS, "A", "B", 100 * PX, 5, 11, flags=LAST)
        e.finish()
        assert by_order(e)[11]["chain_index"] == 0
        assert e.lifecycle_stats()[2] == 0  # no refill slots opened

    def test_snapshot_add_never_links(self):
        e = eng()
        self.base(e)
        e.push(2_000 + 1 * MS, "A", "B", 100 * PX, 5, 11, flags=SNAP | LAST)
        e.finish()
        assert by_order(e)[11]["chain_index"] == 0

    def test_smaller_refill_links_with_discounted_confidence(self):
        e = eng()
        self.base(e)
        e.push(2_000, "A", "B", 100 * PX, 3, 11, flags=LAST)  # instant, size < clip
        e.push(9 * MS, "C", "B", 100 * PX, 3, 11, flags=LAST)
        e.finish()
        r = by_order(e)[11]
        assert r["chain_index"] == 1
        assert r["link_confidence"] == pytest.approx(0.75)  # 0.75 size x 1.0 time


class TestDeterminismAndFlags:
    def seq(self, e):
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=SNAP)
        e.push(1_000, "A", "A", 102 * PX, 4, 2, flags=SNAP | LAST)
        e.push(2_000, "A", "B", 99 * PX, 3, 3, flags=LAST)
        e.push(3_000, "T", "A", 100 * PX, 2, 0)
        e.push(3_000, "F", "B", 100 * PX, 2, 1)
        e.push(3_000, "M", "B", 100 * PX, 3, 1, flags=LAST)
        e.push(4_000, "M", "B", 98 * PX, 3, 3, flags=LAST)
        e.push(5_000, "C", "B", 98 * PX, 3, 3, flags=LAST)
        e.finish()

    def test_replay_twice_identical_records_and_digest(self):
        a, b = eng(), eng()
        self.seq(a)
        self.seq(b)
        assert a.lifecycle_digest() == b.lifecycle_digest()
        ca, cb = drain(a), drain(b)
        assert ca.keys() == cb.keys()
        for k in ca:
            assert np.array_equal(ca[k], cb[k]), k

    def test_digest_sensitive_to_lifecycle_difference(self):
        a, b = eng(), eng()
        self.seq(a)
        # same book outcome, different lifetime for order 3
        b.push(1_000, "A", "B", 100 * PX, 5, 1, flags=SNAP)
        b.push(1_000, "A", "A", 102 * PX, 4, 2, flags=SNAP | LAST)
        b.push(2_500, "A", "B", 99 * PX, 3, 3, flags=LAST)  # later add
        b.push(3_000, "T", "A", 100 * PX, 2, 0)
        b.push(3_000, "F", "B", 100 * PX, 2, 1)
        b.push(3_000, "M", "B", 100 * PX, 3, 1, flags=LAST)
        b.push(4_000, "M", "B", 98 * PX, 3, 3, flags=LAST)
        b.push(5_000, "C", "B", 98 * PX, 3, 3, flags=LAST)
        b.finish()
        assert a.lifecycle_digest() != b.lifecycle_digest()

    def test_snapshot_flag_carried(self):
        e = eng()
        self.seq(e)
        r = by_order(e)
        assert r[1]["from_snapshot"] == 1
        assert r[3]["from_snapshot"] == 0

    def test_lifecycle_disabled_engine_unaffected(self):
        e = gc_core.MboEngine(lifecycle=False)
        e.push(1_000, "A", "B", 100 * PX, 5, 1, flags=LAST)
        e.finish()
        assert e.lifecycle_digest() is None
        with pytest.raises(ValueError):
            e.lifecycle_drain()

    def test_phase1_digest_unchanged_by_lifecycle(self):
        a = gc_core.MboEngine(lifecycle=False)
        b = gc_core.MboEngine(lifecycle=True)
        for x in (a, b):
            self.seq(x)
        assert a.state_digest() == b.state_digest()
        assert dict(a.stats()) == dict(b.stats())
