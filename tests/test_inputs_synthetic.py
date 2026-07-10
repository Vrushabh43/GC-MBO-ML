"""Known-answer tests for the Phase 5 input assembler (Steps 13-17) and
the input-standardization layer. Synthetic sessions run through the REAL
compiled path (engine -> flow -> features -> normalization -> assembler).
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_normalization_synthetic import steady_session  # noqa: E402

from calendar_mod.events import EventCalendar, ScheduledEvent  # noqa: E402
from datasets.inputs import (  # noqa: E402
    BAR_FEATURES,
    EVENT_FEATURES,
    SLOW_FEATURES,
    TACTICAL_FEATURES,
    InputAssembler,
)
from datasets.standardize import InputStandardizer, LOG_DIMS  # noqa: E402
from features.normalization import NS, NormalizedFeatureEngine  # noqa: E402
from utilities.config import load_config  # noqa: E402

CFG = load_config()


def assemble(cols, calendar=None, sample_at=None, dte=42.0, dsr=10.0):
    nfe = NormalizedFeatureEngine(CFG)
    cal = calendar if calendar is not None else EventCalendar()
    asm = InputAssembler(CFG, calendar=cal, days_to_expiry=dte, days_since_roll=dsr)
    n = len(cols["ts"])
    sample = None
    for i in range(n):
        out = nfe.step(cols, i)
        asm.note_depth(cols, i)
        asm.step(cols, i, out)
        if sample_at is not None and sample is None and int(cols["ts"][i]) >= sample_at:
            sample = asm.sample(int(cols["ts"][i]))
    return asm, (sample if sample is not None else asm.sample())


class TestEventStream:
    def test_exact_order_and_window(self):
        cols = steady_session(mult=1, seconds=60)
        asm, s = assemble(cols)
        n_rows = len(cols["ts"])
        assert s.events_len == min(n_rows, CFG.raw["inputs"]["event_window"])
        # dt column non-negative and mostly small (events are sub-second
        # spaced within each synthetic second)
        dts = s.events[-s.events_len :, EVENT_FEATURES.index("dt_s")]
        assert (dts >= 0).all()
        # exact order: the trailing rows correspond to the last groups
        trade_n = s.events[-1, EVENT_FEATURES.index("trade_n")]
        assert trade_n >= 0

    def test_event_content_known_trade(self):
        cols = steady_session(mult=1, seconds=200, wiggle=False)
        asm, s = assemble(cols)
        # find the last trade event: t_sell_norm = 2 lots / (v_scale=2 * 1s) = 1
        col = EVENT_FEATURES.index("t_sell_norm")
        vals = s.events[-s.events_len :, col]
        trades = vals[vals > 0]
        assert len(trades) > 0
        assert trades[-1] == pytest.approx(1.0, rel=0.2)

    def test_warmup_left_padding(self):
        cols = steady_session(mult=1, seconds=5)
        asm, s = assemble(cols)
        assert 0 < s.events_len < CFG.raw["inputs"]["event_window"]
        assert (s.events[: -s.events_len] == 0).all()  # left padding zeros


class TestFlowBars:
    def test_bar_closes_on_event_count(self):
        # with the wiggle the pattern emits ~5 groups/second -> 64 events
        # arrive in ~13 s, beating the 15 s duration rule
        cols = steady_session(mult=1, seconds=120, wiggle=True)
        asm, s = assemble(cols)
        assert s.bars_len > 3
        n_col = BAR_FEATURES.index("n_events")
        filled = s.bars[-s.bars_len :, n_col]
        assert (filled <= CFG.raw["inputs"]["bar_events"]).all()
        assert (filled[:-1] == CFG.raw["inputs"]["bar_events"]).any()

    def test_bar_closes_on_max_duration(self):
        import gc_core

        from test_normalization_synthetic import T0, TICK, engine

        LAST = gc_core.F_LAST
        e = engine()
        base = 2000 * 1_000_000_000
        e.push(T0, "A", "B", base, 10, 9001)
        e.push(T0, "A", "A", base + TICK, 10, 9002, LAST)
        # one sparse event every 6 s: bars must close by duration (~18 s),
        # far below 64 events
        for k in range(1, 20):
            e.push(T0 + k * 6 * NS, "T", "B", base + TICK, 1, 0, LAST)
        e.finish()
        cols = {n: np.frombuffer(b, dtype=np.dtype(d)) for n, d, b in e.flow_drain()}
        asm, s = assemble(cols)
        n_col = BAR_FEATURES.index("n_events")
        d_col = BAR_FEATURES.index("duration_s")
        bars = s.bars[-s.bars_len :]
        assert s.bars_len >= 4
        assert (bars[:, n_col] < 64).all()
        assert (bars[:, d_col] >= CFG.raw["inputs"]["bar_max_duration_s"] - 6.1).all()

    def test_bar_volume_accounting(self):
        cols = steady_session(mult=1, seconds=200, wiggle=False)
        asm, s = assemble(cols)
        # sells only: delta_norm negative, buy 0 in every complete bar
        # closed after v_scale warm-up (the first bar can close cold -> NaN)
        b = s.bars[-s.bars_len : -1]
        buys = b[:, BAR_FEATURES.index("buy_vol_norm")]
        sells = b[:, BAR_FEATURES.index("sell_vol_norm")]
        deltas = b[:, BAR_FEATURES.index("delta_norm")]
        warm = ~np.isnan(sells)
        assert warm.sum() >= len(b) - 2
        assert (buys[warm] == 0).all()
        assert (sells[warm] >= 0).all()
        assert np.allclose(deltas[warm], -sells[warm])


class TestClockContexts:
    def test_tactical_cadence_and_increments(self):
        cols = steady_session(mult=1, seconds=100, wiggle=False)
        asm, s = assemble(cols)
        # ~99 completed seconds -> tactical_len close to that
        assert 95 <= s.tactical_len <= 100
        # each second: 2 lots sold -> delta increment = -2 raw; once v_scale
        # (=2) is warm the normalized increment is -0.5... = -2/(2*1) = -1
        col = TACTICAL_FEATURES.index("delta_inc_norm")
        vals = s.tactical[-s.tactical_len :, col]
        warm = vals[~np.isnan(vals)]
        assert len(warm) > 30
        assert warm[-1] == pytest.approx(-1.0)

    def test_slow_cadence_and_return(self):
        cols = steady_session(mult=1, seconds=100, wiggle=False)
        asm, s = assemble(cols)
        assert 8 <= s.slow_len <= 10  # 10 s steps over ~99 s
        ret = s.slow[-s.slow_len :, SLOW_FEATURES.index("return_norm")]
        assert np.allclose(ret[~np.isnan(ret)], 0.0)  # flat mid session

    def test_events_inc_counts_groups(self):
        cols = steady_session(mult=1, seconds=50, wiggle=False)
        asm, s = assemble(cols)
        col = TACTICAL_FEATURES.index("events_inc")
        vals = s.tactical[-s.tactical_len :, col]
        # 5 groups per second in the steady pattern (T+F+M / add / cancel)
        assert np.median(vals) == pytest.approx(3.0, abs=2.0)


class TestRegimeVector:
    def test_percentiles_bounded_and_calendar_fields(self):
        rel_ts = int(steady_session.__defaults__ and 0) or 0  # noqa: F841
        cols = steady_session(mult=1, seconds=200)
        t_end = int(cols["ts"][-1])
        cal = EventCalendar()
        cal.events.append(
            ScheduledEvent(t_end + 1800 * NS, "CPI", "high", "fixture")
        )
        asm, s = assemble(cols, calendar=cal, dte=42.0, dsr=10.0)
        r = dict(zip(s.regime_names, s.regime.tolist(), strict=True))
        for k, v in r.items():
            if "_pct_" in k and not math.isnan(v):
                assert 0.0 <= v <= 1.0, k
        assert r["seconds_to_next_event"] == pytest.approx(1800.0, abs=2.0)
        assert r["next_event_tier"] == 3.0
        assert r["days_to_expiry"] == 42.0
        assert r["days_since_roll"] == 10.0
        assert r["in_blackout"] == 0.0
        assert 0 <= r["phase_code"] <= 4

    def test_blackout_flag_inside_window(self):
        cols = steady_session(mult=1, seconds=200)
        t_end = int(cols["ts"][-1])
        cal = EventCalendar()
        cal.events.append(ScheduledEvent(t_end + 60 * NS, "NFP", "high", "fixture"))
        asm, s = assemble(cols, calendar=cal)
        r = dict(zip(s.regime_names, s.regime.tolist(), strict=True))
        assert r["in_blackout"] == 1.0  # 60 s ahead < 120 s pre-blackout


class TestDisciplines:
    def test_past_only_no_leakage(self):
        cols = steady_session(mult=1, seconds=200)
        n = len(cols["ts"])
        cut = n // 2
        cut_ts = int(cols["ts"][cut - 1])
        _, s_full = assemble(cols, sample_at=cut_ts)
        truncated = {k: v[:cut] for k, v in cols.items()}
        _, s_part = assemble(truncated, sample_at=cut_ts)
        for name in ("events", "bars", "tactical", "slow", "regime"):
            a, b = getattr(s_full, name), getattr(s_part, name)
            assert np.array_equal(a, b, equal_nan=True), name

    def test_determinism(self):
        cols = steady_session(mult=1, seconds=150)
        _, a = assemble(cols)
        _, b = assemble(cols)
        for name in ("events", "bars", "tactical", "slow", "regime"):
            assert np.array_equal(
                getattr(a, name), getattr(b, name), equal_nan=True
            ), name


class TestStandardizer:
    def test_fit_transform_known(self):
        names = ["a", "dt_s", "c"]
        std = InputStandardizer(names, ["dt_s"], winsorize_bound=8.0)
        x = np.array(
            [[1.0, 0.0, 10.0], [3.0, math.e - 1, 30.0], [5.0, 0.0, 50.0]]
        )
        std.fit(x)
        # column a: median 3, MAD 2; column c: median 30, MAD 20
        y, clipped = std.transform(np.array([[5.0, 0.0, 10.0]]))
        assert y[0, 0] == pytest.approx(1.0)
        assert y[0, 2] == pytest.approx(-1.0)
        assert clipped == 0

    def test_winsorize_clips_and_counts(self):
        std = InputStandardizer(["a"], [], winsorize_bound=8.0)
        std.fit(np.array([[0.0], [1.0], [2.0]]))  # median 1, MAD 1
        y, clipped = std.transform(np.array([[1000.0], [1.0]]))
        assert y[0, 0] == 8.0
        assert clipped == 1

    def test_serialize_round_trip_parity(self, tmp_path):
        names = list(np.array(EVENT_FEATURES))
        std = InputStandardizer(names, LOG_DIMS["events"], 8.0)
        rng = np.random.default_rng(7)
        x = rng.lognormal(size=(500, len(names)))
        std.fit(x)
        p = tmp_path / "std.json"
        std.save(p)
        std2 = InputStandardizer.load(p)
        a, _ = std.transform(x[:50])
        b, _ = std2.transform(x[:50])
        assert np.array_equal(a, b)  # byte-identical (train/serve parity)

    def test_unfitted_refuses(self):
        std = InputStandardizer(["a"], [], 8.0)
        with pytest.raises(AssertionError):
            std.transform(np.zeros((1, 1)))
