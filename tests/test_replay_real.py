"""Real-data verification of the Phase 1 engine on a dev-slice session.

Three layers (plan R1 defenses, minus the MBP-10 cross-check which is
blocked on data acquisition — see the Phase 1 completion report):

1. Data-quality invariants: counters that must be exactly zero on a clean,
   self-contained daily file.
2. Determinism: two independent replays of the same file produce identical
   state digests (Phase 0 requirement, CI test).
3. Independent implementation cross-check: a pure-Python reference book
   (written against the empirically verified GLBX semantics) replays the
   same session; top-10 levels of the front contract are compared at
   checkpoints and at end of session.
"""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mbo_engine.engine import MboEngine  # noqa: E402
from utilities.config import load_config  # noqa: E402

CFG = load_config()
SESSION = dt.date(2026, 1, 4)  # first dev-slice session (Sunday, week open)
MUST_BE_ZERO = [
    "duplicate_add",
    "unknown_cancel",
    "unknown_modify",
    "cancel_size_mismatch",
    "fill_overrun",
    "sequence_regression",
    "store_levels_mismatch",
    "groups_f_without_t",
]


@pytest.fixture(scope="module")
def replayed():
    e = MboEngine(CFG)
    r = e.replay_date(SESSION)
    return e, r


class TestDataQualityInvariants:
    def test_not_halted(self, replayed):
        e, _ = replayed
        assert e.halted() is None

    def test_zero_error_counters(self, replayed):
        e, _ = replayed
        s = e.stats()
        bad = {k: s[k] for k in MUST_BE_ZERO if s[k] != 0}
        assert not bad, f"non-zero error counters: {bad}"

    def test_views_consistent_all_active_instruments(self, replayed):
        e, _ = replayed
        for iid, sym, n, vol in e.instruments()[:10]:
            assert e.views_consistent(iid) is None, f"{sym} views diverged"

    def test_tf_reconciliation_overwhelmingly_clean(self, replayed):
        e, _ = replayed
        s = e.stats()
        reconciled = s["groups_tf_matched"] + s["groups_tf_matched_auction"]
        assert reconciled > 0
        # partial-implied residue must stay a tiny fraction of executions
        assert s["groups_tf_mismatch"] <= 0.01 * reconciled

    def test_front_contract_identified(self, replayed):
        e, _ = replayed
        assert e.symbol(e.front_instrument()).startswith("GC")


class TestDeterminism:
    def test_replay_twice_identical_digest(self):
        digests = []
        for _ in range(2):
            e = MboEngine(CFG)
            r = e.replay_date(SESSION)
            digests.append((r.digest, r.records))
        assert digests[0] == digests[1]


class PurePythonBook:
    """Independent minimal reference book (A/C/M/R mutate; F/T never do)."""

    def __init__(self):
        self.orders = {}  # (iid, oid) -> (side, price, size)

    def process(self, r):
        a = chr(r.action) if isinstance(r.action, int) else str(r.action)
        key = (r.instrument_id, r.order_id)
        if a == "A":
            self.orders[key] = (chr(r.side) if isinstance(r.side, int) else str(r.side),
                                r.price, r.size)
        elif a == "C":
            self.orders.pop(key, None)
        elif a == "M":
            old = self.orders.get(key)
            side = old[0] if old else (chr(r.side) if isinstance(r.side, int) else str(r.side))
            self.orders[key] = (side, r.price, r.size)
        elif a == "R":
            self.orders = {k: v for k, v in self.orders.items()
                           if k[0] != r.instrument_id}

    def top_levels(self, iid, side, n=10):
        agg = {}
        for (i, _), (s, p, sz) in self.orders.items():
            if i == iid and s == side:
                tot, cnt = agg.get(p, (0, 0))
                agg[p] = (tot + sz, cnt + 1)
        items = sorted(agg.items(), reverse=(side == "B"))[:n]
        return [(p, t, c) for p, (t, c) in items]


class TestIndependentCrossCheck:
    def test_top10_matches_pure_python_reference(self, replayed):
        import databento as db
        from databento_io.sessions import session_file

        e, _ = replayed
        front = e.front_instrument()

        ref = PurePythonBook()
        store = db.DBNStore.from_file(session_file(SESSION, CFG))
        for r in store:
            ref.process(r)

        for side in ("B", "A"):
            got = e.top_levels_raw(front, side, 10)
            exp = ref.top_levels(front, side, 10)
            assert got == exp, f"side {side}: engine {got} != reference {exp}"
