"""Real-data verification of the Phase 5 model inputs on a dev-slice
session: shapes and ring occupancy, bar rule compliance, tensor sanity
post-warm-up, ledger/calendar integration, determinism, and the
standardization layer end-to-end on real tensors.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calendar_mod.roll_ledger import RollLedger  # noqa: E402
from datasets.inputs import (  # noqa: E402
    BAR_FEATURES,
    EVENT_FEATURES,
    SLOW_FEATURES,
    TACTICAL_FEATURES,
    assemble_session,
)
from datasets.standardize import InputStandardizer, LOG_DIMS  # noqa: E402
from features.flow_stream import replay_session_flow  # noqa: E402
from utilities.config import load_config  # noqa: E402

CFG = load_config()
SESSION = dt.date(2026, 1, 4)


@pytest.fixture(scope="module")
def assembled():
    fs = replay_session_flow(SESSION, CFG)
    asm, samples = assemble_session(SESSION, CFG, sample_every_s=600, cols=fs.cols)
    return fs, asm, samples


class TestShapesAndContent:
    def test_full_rings_after_warmup(self, assembled):
        _, _, samples = assembled
        s = samples[-1]
        ic = CFG.raw["inputs"]
        assert s.events.shape == (ic["event_window"], len(EVENT_FEATURES))
        assert s.bars.shape == (ic["bar_window"], len(BAR_FEATURES))
        assert s.tactical.shape == (ic["tactical_steps"], len(TACTICAL_FEATURES))
        assert s.slow.shape == (ic["slow_steps"], len(SLOW_FEATURES))
        assert s.events_len == ic["event_window"]
        assert s.tactical_len == ic["tactical_steps"]
        assert s.norm_ready

    def test_no_nan_in_ready_sample_tensors(self, assembled):
        _, _, samples = assembled
        s = samples[-1]
        assert s.norm_ready
        for name in ("events", "bars", "tactical", "slow"):
            arr = getattr(s, name)
            n = getattr(s, f"{name}_len")
            assert not np.isnan(arr[-min(n, 50) :]).any(), name  # recent rows warm
        assert not np.isnan(s.regime).any()

    def test_event_stream_plausible(self, assembled):
        _, _, samples = assembled
        s = samples[-1]
        ev = s.events[-s.events_len :]
        dts = ev[:, EVENT_FEATURES.index("dt_s")]
        assert (dts >= 0).all()
        spreads = ev[:, EVENT_FEATURES.index("spread_ticks")]
        assert np.nanmedian(spreads) < 10  # GC trades near 1-2 ticks RTH

    def test_bars_respect_event_cap(self, assembled):
        _, _, samples = assembled
        s = samples[-1]
        n = s.bars[-s.bars_len :, BAR_FEATURES.index("n_events")]
        assert (n <= CFG.raw["inputs"]["bar_events"]).all()
        assert (n > 0).all()
        d = s.bars[-s.bars_len :, BAR_FEATURES.index("duration_s")]
        assert (d > 0).all()
        # duration can overshoot the cap only by quiet-gap length; the
        # majority of RTH bars close at/under the rule
        assert np.median(d) <= CFG.raw["inputs"]["bar_max_duration_s"] + 1e-6

    def test_regime_vector_bounds_and_ledger(self, assembled):
        _, _, samples = assembled
        s = samples[-1]
        r = dict(zip(s.regime_names, s.regime.tolist(), strict=True))
        for k, v in r.items():
            if "_pct" in k:
                assert 0.0 <= v <= 1.0, (k, v)
        led = RollLedger.load(CFG).active(SESSION)
        assert r["days_to_expiry"] == float(led.days_to_expiry)
        assert r["days_since_roll"] >= 0
        assert r["next_event_tier"] == 3.0  # next scheduled: FOMC (high)
        assert r["phase_code"] in (0.0, 1.0, 2.0, 3.0, 4.0)

    def test_tactical_and_slow_rows_bounded_where_bounded(self, assembled):
        _, _, samples = assembled
        s = samples[-1]
        ta = s.tactical[-s.tactical_len :]
        for name in ("absorption_bid_s", "sweep_failure_score",
                     "queue_depletion_bid_s", "liquidity_vacuum_up",
                     "book_resiliency_bid"):
            col = ta[:, TACTICAL_FEATURES.index(name)]
            assert (col >= 0).all() and (col <= 1).all(), name


class TestDisciplines:
    def test_sample_determinism(self, assembled):
        fs, _, samples = assembled
        _, again = assemble_session(SESSION, CFG, sample_every_s=600, cols=fs.cols)
        assert len(samples) == len(again)
        for a, b in zip(samples, again, strict=True):
            for name in ("events", "bars", "tactical", "slow", "regime"):
                assert np.array_equal(
                    getattr(a, name), getattr(b, name), equal_nan=True
                ), name


class TestStandardizerOnRealTensors:
    def test_fit_transform_real_tactical(self, assembled):
        _, _, samples = assembled
        ready = [s for s in samples if s.norm_ready]
        stack = np.stack([s.tactical for s in ready])  # [n, 300, F]
        std = InputStandardizer(TACTICAL_FEATURES, LOG_DIMS["tactical"], 8.0)
        std.fit(stack)
        y, clipped = std.transform(ready[-1].tactical)
        assert y.shape == ready[-1].tactical.shape
        assert np.abs(y[~np.isnan(y)]).max() <= 8.0
        # winsorization touches tails only: <5% across the session's
        # samples (single atypical windows may clip more — clip counts are
        # the Phase 10 drift signal, not an error)
        _, clipped_all = std.transform(stack)
        assert clipped_all < stack.size * 0.05
        assert clipped < y.size * 0.15

    def test_fit_transform_real_regime(self, assembled):
        _, _, samples = assembled
        ready = [s for s in samples if s.norm_ready]
        names = ready[0].regime_names
        stack = np.stack([s.regime for s in ready])
        std = InputStandardizer(names, LOG_DIMS["regime"], 8.0).fit(stack)
        y, _ = std.transform(stack)
        # the layer never CREATES NaN (input NaNs pass through for masking)
        assert np.isnan(y).sum() <= np.isnan(stack).sum()
        ok = ~np.isnan(y)
        assert np.abs(y[ok]).max() <= 8.0
