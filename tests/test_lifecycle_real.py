"""Real-data verification of the Phase 2 lifecycle/queue engine on a
dev-slice session.

Layers:
1. Conservation invariants: every add terminates exactly once; lifecycle
   fill volume reconciles EXACTLY with the engine's F volume; termination
   states reconcile exactly with the Phase 1 cancel-classification counters.
2. Sanity of queue/distance measurements on real market data.
3. Iceberg chain heuristic: links exist at a plausible rate, respect the
   window, and carry confidences.
4. Determinism: replay-twice identical lifecycle digests (CI requirement).
"""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mbo_engine.engine import MboEngine  # noqa: E402
from queue_engine.lifecycle import (  # noqa: E402
    chains_frame,
    orders_frame,
    session_diagnostics,
)
from utilities.config import load_config  # noqa: E402

CFG = load_config()
SESSION = dt.date(2026, 1, 4)  # first dev-slice session


@pytest.fixture(scope="module")
def replayed():
    e = MboEngine(CFG, lifecycle=True)
    r = e.replay_date(SESSION)
    stats = e.stats()
    lstats = e.lifecycle_stats()
    digest = e.lifecycle_digest()
    orders = orders_frame(e.lifecycle_drain(), CFG)
    front = e.front_instrument()
    return r, stats, lstats, digest, orders, front


class TestConservation:
    def test_every_add_terminates_exactly_once(self, replayed):
        _, stats, lstats, _, orders, _ = replayed
        # adds + unknown-modify synthetic adds + snapshot adds are all adds;
        # every one must appear exactly once in the lifecycle output
        expected = stats["adds"] + stats["unknown_modify"]
        assert lstats["records_emitted"] == expected
        assert len(orders) == expected
        assert orders.duplicated(["instrument_id", "order_id", "ts_added"]).sum() == 0

    def test_fill_volume_reconciles_exactly(self, replayed):
        _, stats, _, _, orders, _ = replayed
        # F records only update resting-order state (Phase 1 rule); the sum
        # of per-order fill attributions must equal total F volume minus the
        # (tiny, counted) volume of fills against unknown order ids
        assert (
            int(orders["filled_size"].sum())
            == stats["f_volume"] - stats["f_volume_unattributed"]
        )
        assert stats["f_volume_unattributed"] < 100  # implied-leg edge only

    def test_states_reconcile_with_cancel_classification(self, replayed):
        _, stats, _, _, orders, _ = replayed
        by_state = orders["final_state"].value_counts()
        assert by_state.get("filled", 0) == stats["cancels_fill_removal"]
        pulled = by_state.get("cancelled", 0) + by_state.get("partial_cancelled", 0)
        assert pulled == stats["cancels_pulled"]
        assert by_state.get("replaced", 0) == stats["duplicate_add"]
        # a real session file ends with a resting book
        assert by_state.get("end_of_data", 0) > 0

    def test_filled_orders_actually_filled(self, replayed):
        _, _, _, _, orders, _ = replayed
        filled = orders[orders["final_state"] == "filled"]
        assert (filled["filled_size"] > 0).all()
        pure_cancel = orders[orders["final_state"] == "cancelled"]
        assert (pure_cancel["filled_size"] == 0).all()


class TestQueueAndDistanceSanity:
    def test_queue_positions_present_and_bounded(self, replayed):
        _, _, _, _, orders, front = replayed
        f = orders[orders["instrument_id"] == front]
        assert f["queue_pos_at_add"].notna().all()
        # non-snapshot adds join the back of visible queues; positions are
        # small relative to total session order flow
        assert (f["queue_pos_at_add"] < 100_000).all()
        # front-of-queue terminations must exist (fills deplete from front)
        term = f["queue_pos_at_term"].dropna()
        assert (term == 0).sum() > 0

    def test_fills_terminate_at_queue_front_mostly(self, replayed):
        _, _, _, _, orders, front = replayed
        f = orders[(orders["instrument_id"] == front)
                   & (orders["final_state"] == "filled")]
        pos = f["queue_pos_at_term"].dropna()
        # FIFO matching: the overwhelming majority of full fills remove the
        # front of the queue (exceptions: multi-order sweeps within one event
        # where earlier removals in the same group already advanced the queue)
        assert (pos == 0).mean() > 0.95

    def test_distances_sane_on_front_contract(self, replayed):
        _, _, _, _, orders, front = replayed
        f = orders[(orders["instrument_id"] == front) & ~orders["from_snapshot"]]
        d = f["dist_same_at_add_ticks"].dropna()
        assert len(d) > 50_000  # Sunday session: ~88k front-contract adds
        # continuous-session adds rest at or behind the same-side best;
        # crossed pre-open books may produce a small negative share
        assert (d >= 0).mean() > 0.95
        assert d.median() < 500  # GC liquidity concentrates near the touch

    def test_survival_measurement_populated(self, replayed):
        _, _, _, _, orders, front = replayed
        f = orders[(orders["instrument_id"] == front) & ~orders["from_snapshot"]]
        assert f["min_dist_same_ticks"].notna().mean() > 0.99
        # closest approach can never exceed the distance at add measured at
        # the same price (approach only tightens); allow half-tick rounding
        both = f[f["n_price_changes"] == 0].dropna(
            subset=["min_dist_same_ticks", "dist_same_at_add_ticks"]
        )
        assert (both["min_dist_same_ticks"]
                <= both["dist_same_at_add_ticks"] + 1e-9).all()
        assert f["touched_best"].sum() > 0
        assert f["cancel_before_touch"].sum() > 0

    def test_session_diagnostics_neutral_and_bounded(self, replayed):
        _, _, _, _, orders, front = replayed
        diag = session_diagnostics(orders, front)
        for key in ("cancel_before_touch_rate", "liquidity_survival_ratio",
                    "short_lived_large_order_behavior", "fill_rate",
                    "refill_link_share_of_adds"):
            assert 0.0 <= diag[key] <= 1.0, key
        assert diag["median_lifetime_ms_chain_adjusted"] >= 0


class TestIcebergChains:
    def test_links_exist_at_plausible_rate(self, replayed):
        _, _, lstats, _, orders, _ = replayed
        assert lstats["iceberg_links_made"] > 0
        # refill links are a small minority of adds (heuristic sanity)
        share = lstats["iceberg_links_made"] / max(len(orders), 1)
        assert share < 0.10

    def test_links_respect_window_and_confidence(self, replayed):
        _, _, _, _, orders, _ = replayed
        links = orders[orders["chain_index"] > 0]
        assert len(links) > 0
        assert (links["link_dt_ns"] <= CFG.lifecycle.iceberg_link_window_ns).all()
        assert (links["link_confidence"] > 0).all()
        assert (links["link_confidence"] <= 1.0).all()

    def test_chain_table_consistent(self, replayed):
        _, _, _, _, orders, _ = replayed
        chains = chains_frame(orders)
        assert len(chains) > 0
        assert (chains["members"] >= 2).all()
        assert (chains["refills"] == chains["members"] - 1).all()
        assert (chains["total_displayed"] > 0).all()
        # executed volume on a chain comes from real fills
        assert (chains["total_filled"] > 0).all()


class TestDeterminism:
    def test_replay_twice_identical_lifecycle_digest(self, replayed):
        _, _, _, digest, _, _ = replayed
        e2 = MboEngine(CFG, lifecycle=True)
        e2.replay_date(SESSION)
        assert e2.lifecycle_digest() == digest

    def test_phase1_state_digest_unchanged_by_lifecycle(self, replayed):
        r, _, _, _, _, _ = replayed
        e = MboEngine(CFG, lifecycle=False)
        r0 = e.replay_date(SESSION)
        assert r0.digest == r.digest


class TestIndependentQueueCrossCheck:
    """Pure-Python FIFO queue replica over the front contract's records:
    queue position at add must match the engine's lifecycle output exactly
    (independent implementation of the Globex priority rules)."""

    def test_queue_pos_at_add_matches_reference(self, replayed):
        import databento as db
        from databento_io.sessions import session_file

        _, _, _, _, orders, front = replayed

        fifo: dict[tuple[str, int], list] = {}
        loc: dict[int, tuple[str, int, int]] = {}  # oid -> (side, price, size)
        expected: dict[tuple[int, int], int] = {}  # (oid, ts) -> pos at add

        store = db.DBNStore.from_file(session_file(SESSION, CFG))
        for r in store:
            if r.instrument_id != front:
                continue
            a = chr(r.action) if isinstance(r.action, int) else str(r.action)
            s = chr(r.side) if isinstance(r.side, int) else str(r.side)
            if a == "A":
                q = fifo.setdefault((s, r.price), [])
                expected[(r.order_id, r.ts_event)] = len(q)
                q.append(r.order_id)
                loc[r.order_id] = (s, r.price, r.size)
            elif a == "C":
                if r.order_id in loc:
                    side, p, _ = loc.pop(r.order_id)
                    q = fifo.get((side, p), [])
                    if r.order_id in q:
                        q.remove(r.order_id)
            elif a == "M":
                if r.order_id in loc:
                    side, p, sz = loc[r.order_id]
                    q = fifo.get((side, p), [])
                    if r.price != p or r.size > sz:
                        # price change or size increase: lose priority
                        if r.order_id in q:
                            q.remove(r.order_id)
                        q2 = fifo.setdefault((side, r.price), [])
                        q2.append(r.order_id)
                    loc[r.order_id] = (side, r.price, r.size)
            elif a == "R":
                fifo.clear()
                loc.clear()

        f = orders[orders["instrument_id"] == front]
        got = dict(
            zip(
                zip(f["order_id"].astype(int), f["ts_added"].astype(int),
                    strict=True),
                f["queue_pos_at_add"].astype(int),
                strict=True,
            )
        )
        checked = 0
        for key, exp in expected.items():
            if key in got:
                assert got[key] == exp, f"order {key}: engine {got[key]} != ref {exp}"
                checked += 1
        assert checked > 50_000  # meaningful coverage (Sunday session)
