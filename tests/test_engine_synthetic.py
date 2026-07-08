"""Synthetic-exchange tests for the compiled MBO engine (plan R1 defense c).

Every test feeds a hand-constructed event sequence with a book state known
by construction and asserts both views (order store and price levels),
Globex priority rules, the T/F reconciliation rule, protections, and
invariants. Prices are DBN fixed-point ints (1e-9); sizes are contracts.
"""
import gc_core
import pytest

PX = 1_000_000_000  # 1.0 point in fixed-point
LAST = gc_core.F_LAST
SNAP = gc_core.F_SNAPSHOT


def eng(**kw):
    return gc_core.MboEngine(**kw)


def stats(e):
    return dict(e.stats())


class TestBookConstruction:
    def test_add_best_and_both_views(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1)
        e.push(2, "A", "B", 4299 * PX, 7, 2)
        e.push(3, "A", "B", 4300 * PX, 2, 3)
        e.push(4, "A", "A", 4301 * PX, 4, 4)
        e.push(5, "A", "A", 4302 * PX, 9, 5, flags=LAST)
        e.finish()

        (bid, ask) = e.best_bid_ask(1)
        assert bid == (4300 * PX, 7, 2)  # 5+2 contracts, 2 orders
        assert ask == (4301 * PX, 4, 1)

        lv = e.top_levels(1, "B", 10)
        ov = e.top_levels(1, "B", 10, True)
        assert lv == ov == [(4300 * PX, 7, 2), (4299 * PX, 7, 1)]
        lv_a = e.top_levels(1, "A", 10)
        assert lv_a == [(4301 * PX, 4, 1), (4302 * PX, 9, 1)]
        assert e.views_consistent(1) is None

    def test_cancel_removes_order_and_level(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1)
        e.push(2, "A", "B", 4299 * PX, 7, 2, flags=LAST)
        e.push(3, "C", "B", 4300 * PX, 5, 1, flags=LAST)
        e.finish()
        assert e.order(1, 1) is None
        assert e.best_bid_ask(1) is None  # no asks -> None; check levels instead
        assert e.top_levels(1, "B", 10) == [(4299 * PX, 7, 1)]
        assert stats(e)["unknown_cancel"] == 0
        assert e.views_consistent(1) is None

    def test_clear_resets_instrument(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1)
        e.push(2, "A", "A", 4301 * PX, 5, 2, flags=LAST)
        e.push(3, "R", "N", gc_core.UNDEF_PRICE, 0, 0, flags=LAST)
        e.finish()
        assert e.top_levels(1, "B", 10) == []
        assert e.top_levels(1, "A", 10) == []
        assert e.order_count(1) == 0
        assert stats(e)["clears"] == 1

    def test_instruments_isolated(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1, instrument_id=10, flags=LAST)
        e.push(2, "A", "B", 9999 * PX, 3, 2, instrument_id=20, flags=LAST)
        e.finish()
        assert e.top_levels(10, "B", 10) == [(4300 * PX, 5, 1)]
        assert e.top_levels(20, "B", 10) == [(9999 * PX, 3, 1)]


class TestFills:
    """Verified GLBX MBO semantics: F is execution attribution only; the
    book mutation arrives as the follow-up C (full fill) or M (partial fill)
    in the same matching event."""

    def test_partial_fill_via_f_then_m(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 10, 1, flags=LAST)
        # partial fill 4: T (aggressor sell) + F attribution + M book change
        e.push(2, "T", "A", 4300 * PX, 4, 0)
        e.push(2, "F", "B", 4300 * PX, 4, 1)
        # F alone must NOT have mutated the book
        assert e.top_levels(1, "B", 10) == [(4300 * PX, 10, 1)]
        e.push(2, "M", "B", 4300 * PX, 6, 1, flags=LAST)
        o = e.order(1, 1)
        assert o is not None
        side, price, cur, init, filled, ts_add, ts_upd, state, snap = o
        assert (side, cur, init, filled, state) == ("B", 6, 10, 4, 1)
        assert e.top_levels(1, "B", 10) == [(4300 * PX, 6, 1)]
        assert e.queue_position(1, 1) == 0  # size decrease keeps priority
        # full fill of remainder: T + F + C (fill-removal)
        e.push(3, "T", "A", 4300 * PX, 6, 0)
        e.push(3, "F", "B", 4300 * PX, 6, 1)
        e.push(3, "C", "B", 4300 * PX, 6, 1, flags=LAST)
        e.finish()
        assert e.order(1, 1) is None
        assert e.top_levels(1, "B", 10) == []
        s = stats(e)
        assert s["t_volume"] == 10 and s["f_volume"] == 10
        assert s["t_volume_sell"] == 10 and s["t_volume_buy"] == 0
        assert s["groups_tf_matched"] == 2 and s["groups_tf_mismatch"] == 0
        assert s["cancels_fill_removal"] == 1 and s["cancels_pulled"] == 0
        assert s["unknown_cancel"] == 0 and s["fill_overrun"] == 0
        assert e.views_consistent(1) is None

    def test_cancel_classification_pull_vs_fill_removal(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1, flags=LAST)
        e.push(2, "A", "B", 4300 * PX, 5, 2, flags=LAST)
        # order 1: executed (F then C, same matching event)
        e.push(3, "T", "A", 4300 * PX, 5, 0)
        e.push(3, "F", "B", 4300 * PX, 5, 1)
        e.push(3, "C", "B", 4300 * PX, 5, 1, flags=LAST)
        # order 2: trader pull (bare C)
        e.push(4, "C", "B", 4300 * PX, 5, 2, flags=LAST)
        e.finish()
        s = stats(e)
        assert s["cancels_fill_removal"] == 1
        assert s["cancels_pulled"] == 1

    def test_iceberg_fill_exceeding_displayed_counted(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 3, 1, flags=LAST)
        e.push(2, "T", "A", 4300 * PX, 5, 0)
        e.push(2, "F", "B", 4300 * PX, 5, 1, flags=LAST)  # hidden qty executes
        e.finish()
        s = stats(e)
        assert s["fills_exceeding_displayed"] == 1
        assert s["fill_overrun"] == 0  # not an incident
        # book untouched by F; order still resting with original size
        assert e.top_levels(1, "B", 10) == [(4300 * PX, 3, 1)]
        assert e.views_consistent(1) is None

    def test_auction_uncross_f_equals_2t_reconciles(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1, flags=LAST)
        e.push(2, "A", "A", 4300 * PX, 5, 2, flags=LAST)
        # opening cross: T once, F attribution on BOTH resting sides
        e.push(3, "T", "N", 4300 * PX, 5, 0)
        e.push(3, "F", "B", 4300 * PX, 5, 1)
        e.push(3, "F", "A", 4300 * PX, 5, 2)
        e.push(3, "C", "B", 4300 * PX, 5, 1)
        e.push(3, "C", "A", 4300 * PX, 5, 2, flags=LAST)
        e.finish()
        s = stats(e)
        assert s["groups_tf_matched_auction"] == 1
        assert s["groups_tf_mismatch"] == 0
        assert s["cancels_fill_removal"] == 2
        assert e.order_count(1) == 0

    def test_t_only_volume_rule(self):
        """Aggressive volume comes only from T; F contributes nothing to it."""
        e = eng()
        e.push(1, "A", "A", 4301 * PX, 10, 1, flags=LAST)
        e.push(2, "T", "B", 4301 * PX, 10, 0)
        e.push(2, "F", "A", 4301 * PX, 10, 1)
        e.push(2, "C", "A", 4301 * PX, 10, 1, flags=LAST)
        e.finish()
        s = stats(e)
        assert s["t_volume"] == 10          # not 20 (no double count)
        assert s["t_volume_buy"] == 10      # aggressor side from T


class TestModifyPriorityRules:
    def setup_level(self, e):
        e.push(1, "A", "B", 4300 * PX, 5, 1)
        e.push(2, "A", "B", 4300 * PX, 5, 2)
        e.push(3, "A", "B", 4300 * PX, 5, 3, flags=LAST)
        assert [e.queue_position(1, i) for i in (1, 2, 3)] == [0, 1, 2]

    def test_size_decrease_keeps_priority(self):
        e = eng()
        self.setup_level(e)
        e.push(4, "M", "B", 4300 * PX, 3, 2, flags=LAST)  # 5 -> 3
        e.finish()
        assert e.queue_position(1, 2) == 1  # kept
        assert e.top_levels(1, "B", 1) == [(4300 * PX, 13, 3)]
        assert e.views_consistent(1) is None

    def test_size_increase_loses_priority(self):
        e = eng()
        self.setup_level(e)
        e.push(4, "M", "B", 4300 * PX, 9, 1, flags=LAST)  # front order 5 -> 9
        e.finish()
        assert e.queue_position(1, 1) == 2  # back of queue
        assert e.queue_position(1, 2) == 0
        assert e.top_levels(1, "B", 1) == [(4300 * PX, 19, 3)]

    def test_price_change_moves_level_and_loses_priority(self):
        e = eng()
        self.setup_level(e)
        e.push(4, "A", "B", 4299 * PX, 4, 9, flags=LAST)
        e.push(5, "M", "B", 4299 * PX, 5, 1, flags=LAST)  # order 1: 4300 -> 4299
        e.finish()
        assert e.top_levels(1, "B", 10) == [
            (4300 * PX, 10, 2),
            (4299 * PX, 9, 2),
        ]
        assert e.queue_position(1, 1) == 1  # behind order 9 at new price
        o = e.order(1, 1)
        assert o[1] == 4299 * PX
        assert e.views_consistent(1) is None

    def test_unknown_modify_treated_as_add_and_logged(self):
        e = eng()
        e.push(1, "M", "B", 4300 * PX, 5, 77, flags=LAST)
        e.finish()
        assert stats(e)["unknown_modify"] == 1
        assert e.top_levels(1, "B", 10) == [(4300 * PX, 5, 1)]


class TestProtections:
    def test_duplicate_add_logged_and_replaced(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1, flags=LAST)
        e.push(2, "A", "B", 4299 * PX, 9, 1, flags=LAST)  # same order id!
        e.finish()
        assert stats(e)["duplicate_add"] == 1
        # stale order replaced; views stay synchronized
        assert e.top_levels(1, "B", 10) == [(4299 * PX, 9, 1)]
        assert e.views_consistent(1) is None

    def test_unknown_cancel_and_fill_logged(self):
        e = eng()
        e.push(1, "C", "B", 4300 * PX, 5, 111, flags=LAST)
        e.push(2, "T", "A", 4300 * PX, 2, 0)
        e.push(2, "F", "B", 4300 * PX, 2, 222, flags=LAST)
        e.finish()
        s = stats(e)
        assert s["unknown_cancel"] == 1 and s["unknown_fill"] == 1

    def test_cancel_size_mismatch_logged(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1, flags=LAST)
        e.push(2, "C", "B", 4300 * PX, 3, 1, flags=LAST)  # record says 3, stored 5
        e.finish()
        assert stats(e)["cancel_size_mismatch"] == 1
        assert e.order(1, 1) is None  # C removes the order regardless

    def test_sequence_regression_detected_snapshot_exempt(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 5, 1, sequence=100, flags=LAST)
        e.push(2, "A", "B", 4299 * PX, 5, 2, sequence=99, flags=LAST)  # regression
        e.push(3, "A", "B", 4298 * PX, 5, 3, sequence=5, flags=SNAP | LAST)  # exempt
        e.finish()
        assert stats(e)["sequence_regression"] == 1

    def test_snapshot_records_counted(self):
        e = eng()
        e.push(1, "R", "N", gc_core.UNDEF_PRICE, 0, 0)
        e.push(1, "A", "B", 4300 * PX, 5, 1, flags=SNAP)
        e.push(1, "A", "A", 4301 * PX, 5, 2, flags=SNAP | LAST)
        e.finish()
        s = stats(e)
        assert s["snapshot_records"] == 2
        assert e.top_levels(1, "B", 10) == [(4300 * PX, 5, 1)]


class TestReconciliationAndInvariants:
    def test_tf_mismatch_incident(self):
        e = eng()
        e.push(1, "A", "B", 4300 * PX, 10, 1, flags=LAST)
        e.push(2, "T", "A", 4300 * PX, 5, 0)
        e.push(2, "F", "B", 4300 * PX, 3, 1, flags=LAST)  # F=3 != T=5
        e.finish()
        s = stats(e)
        assert s["groups_tf_mismatch"] == 1
        assert s["tf_reconcile_mismatch"] >= 1
        kinds = [i[0] for i in e.incidents(10)]
        assert "tf_reconcile_mismatch" in kinds

    def test_t_without_f_counted_not_incident(self):
        """Implied/spread executions produce T with no resting fills."""
        e = eng()
        e.push(1, "T", "B", 4300 * PX, 5, 0, flags=LAST)
        e.finish()
        s = stats(e)
        assert s["groups_t_without_f"] == 1
        assert s["tf_reconcile_mismatch"] == 0

    def test_crossed_book_detected_at_group_end(self):
        e = eng()
        e.push(1, "A", "B", 4302 * PX, 5, 1, flags=LAST)
        e.push(2, "A", "A", 4301 * PX, 5, 2, flags=LAST)  # ask below bid
        e.finish()
        assert stats(e)["crossed_book"] >= 1

    def test_transient_crossing_inside_group_not_flagged(self):
        e = eng()
        # both records in ONE matching event; by group end the book is sane
        e.push(1, "A", "B", 4302 * PX, 5, 1)
        e.push(1, "C", "B", 4302 * PX, 5, 1)
        e.push(1, "A", "B", 4300 * PX, 5, 2)
        e.push(1, "A", "A", 4301 * PX, 5, 3, flags=LAST)
        e.finish()
        assert stats(e)["crossed_book"] == 0


class TestDeterminism:
    SEQ = [
        (1, "A", "B", 4300 * PX, 5, 1, 0),
        (2, "A", "A", 4301 * PX, 7, 2, 0),
        (3, "A", "B", 4299 * PX, 3, 3, LAST),
        (4, "T", "A", 4300 * PX, 2, 0, 0),
        (4, "F", "B", 4300 * PX, 2, 1, LAST),
        (5, "M", "B", 4299 * PX, 8, 3, LAST),
        (6, "C", "A", 4301 * PX, 7, 2, LAST),
    ]

    def run(self):
        e = eng()
        for ts, a, s, p, sz, oid, fl in self.SEQ:
            e.push(ts, a, s, p, sz, oid, flags=fl)
        e.finish()
        return e

    def test_push_replay_twice_identical_digest(self):
        assert self.run().state_digest() == self.run().state_digest()

    def test_digest_sensitive_to_state(self):
        e1 = self.run()
        e2 = self.run()
        e2.push(99, "A", "B", 1 * PX, 1, 999, flags=LAST)
        e2.finish()
        assert e1.state_digest() != e2.state_digest()


class TestHaltPolicy:
    def test_engine_halts_only_on_engine_invariant(self):
        # data-quality issues never halt; the store/levels audit would.
        e = eng(halt_on_engine_invariant=True, audit_interval=5)
        for i in range(20):
            e.push(i + 1, "A", "B", (4300 + i) * PX, 5, i + 1, flags=LAST)
        e.finish()
        assert e.halted() is None  # consistent engine never halts

    def test_invalid_side_rejected(self):
        e = eng()
        with pytest.raises(ValueError):
            e.top_levels(1, "X", 5)
