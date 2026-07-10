"""Phase 5 — the five final model inputs (plan Phase 5, Steps 13-17).

ONE streaming consumer (`InputAssembler.step`) ingests the per-group flow
rows plus the Phase 3/4 feature vector and maintains, past-only:

  1. EVENT stream  — last `event_window` matching events x ~22 features.
     GRANULARITY DECISION (documented in the Phase 5 report): "event" =
     one CME matching event (ts_event group), the atomic market action the
     Phase 1 engine reconstructs — not one raw MBO record, which splits a
     single fill into mechanical T/F/C(/M) rows. Exact event order is
     preserved; order-age/queue context enters via best-level liquidity
     age and the group's lifecycle termination fields.
  2. ADAPTIVE FLOW BARS — close after `bar_events` events or
     `bar_max_duration_s`, whichever first; last `bar_window` bars x ~30
     features.
  3. TACTICAL context — last `tactical_steps` seconds at 1 s.
  4. SLOW context — last `slow_steps` steps at 10 s.
  5. REGIME vector — percentile summary (config windows 1m/5m/15m/60m/
     session-cap) of ten activity bases + the Phase 4A scales (raw + pct)
     + Step 12.5 calendar features + days-to-expiry / roll proximity.

Per plan Phase 5: models consume the era-normalized (_norm) variants, so
tensor entries are normalized AT EVENT TIME with the then-current
(past-only) scales; raw values live in the feature store. During scale
warm-up normalized entries are NaN — samples carry `norm_ready` and Phase 6
must not select samples before readiness (Phase 11 warm-up gate).

Quiet-gap semantics (documented): tactical/slow rings carry the last
snapshot through inactive seconds (state persists; per-second increments
are exactly zero); gap-fill is capped at one ring length.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from calendar_mod.events import EventCalendar
from calendar_mod.session_phase import PHASES, PhaseClock
from features.normalization import NS, NormalizedFeatureEngine, RollingPercentile
from utilities.config import Config

# ---------------------------------------------------------------- feature sets

EVENT_FEATURES = [
    "dt_s",                    # inter-event time (raw seconds; log at std-layer)
    "t_buy_norm", "t_sell_norm", "trade_n",
    "trade_px_dist_mid_norm",  # trade price distance from mid, sigma units
    "add_near_b_norm", "add_near_a_norm",
    "pull_near_b_norm", "pull_near_a_norm",
    "fill_b_norm", "fill_a_norm", "hidden_norm",
    "mid_change_norm",         # mid move since previous event, sigma units
    "spread_ticks",            # raw (do-not-normalize)
    "book_imbalance_l1", "microprice_disp_norm",
    "mlofi_1_norm",
    "age_best_b_s", "age_best_a_s",   # raw seconds (log at std-layer)
    "term_filled", "term_pulled", "refill_n",
]

BAR_FEATURES = [
    "duration_s", "n_events", "n_trades",
    "buy_vol_norm", "sell_vol_norm", "delta_norm",
    "add_near_b_norm", "add_near_a_norm", "pull_near_b_norm", "pull_near_a_norm",
    "fill_b_norm", "fill_a_norm", "hidden_norm",
    "replenish_bar_bid", "replenish_bar_ask",     # bounded [0,1]
    "mid_change_norm", "range_norm",              # close-open / high-low, sigma
    "mlofi_1_norm", "mlofi_near_norm",
    "sweep_ticks_norm",
    "term_filled", "term_pulled", "refill_n",
    "events_per_s",
    # state at bar close (bounded/normalized)
    "absorption_net_s", "book_imbalance_near", "spread_ticks",
    "queue_turnover_bid_m", "queue_turnover_ask_m", "microprice_disp_norm",
]

TACTICAL_FEATURES = [
    # per-second increments (exact zeros in quiet seconds)
    "delta_inc_norm", "vol_inc_norm", "events_inc",
    # windowed/state features snapshot (plan input-3 list)
    "aggr_delta_s_norm", "absorption_bid_s", "absorption_ask_s",
    "mlofi_near_s_norm", "price_impact_m_norm",
    "sweep_buy_ticks_m_norm", "sweep_sell_ticks_m_norm", "sweep_failure_score",
    "queue_depletion_bid_s", "queue_depletion_ask_s",
    "liquidity_survival_ratio_l", "replenish_bid_m", "replenish_ask_m",
    "book_resiliency_bid", "book_resiliency_ask",
    "liquidity_vacuum_up", "liquidity_vacuum_down",
    "trade_burst_intensity_s", "mid_change_1s_norm", "spread_ticks",
]

SLOW_FEATURES = [
    "return_norm",            # 10 s mid change, sigma units
    "range_norm",             # 10 s high-low, sigma units (realized vol proxy)
    "delta_inc_norm", "vol_inc_norm", "events_inc",
    "cvd_slope_norm",         # == delta increment per 10 s (normalized)
    "absorption_net_mean", "failed_sweeps_l", "mlofi_near_m_norm",
    "depth_norm",             # near depth / d_scale
    "spread_ticks", "trade_burst_intensity_s",
    "book_resiliency_bid", "book_resiliency_ask", "directional_efficiency_m",
]

# regime bases: name -> how it is sampled each second (see _regime_value)
REGIME_BASES = [
    "event_rate", "buy_vol", "sell_vol", "delta", "volatility", "spread",
    "absorption", "depth", "queue_turnover", "replenishment", "sweep_activity",
]


@dataclass
class InputSample:
    """One assembled model-input sample (all arrays float32, left-padded)."""

    ts: int
    events: np.ndarray          # [event_window, len(EVENT_FEATURES)]
    events_len: int
    bars: np.ndarray            # [bar_window, len(BAR_FEATURES)]
    bars_len: int
    tactical: np.ndarray        # [tactical_steps, len(TACTICAL_FEATURES)]
    tactical_len: int
    slow: np.ndarray            # [slow_steps, len(SLOW_FEATURES)]
    slow_len: int
    regime: np.ndarray          # [len(regime_names)]
    regime_names: list[str] = field(repr=False)
    norm_ready: bool = False


class _BarAcc:
    __slots__ = ("open_ts", "n", "trades", "buy", "sell", "add_nb", "add_na",
                 "pull_nb", "pull_na", "fill_b", "fill_a", "hidden",
                 "add_bb", "add_ba", "pull_bb", "pull_ba",
                 "mid_open", "mid_high", "mid_low",
                 "mlofi1", "mlofi_near", "sweep_ticks",
                 "term_filled", "term_pulled", "refills")

    def __init__(self, ts: int, mid: float | None) -> None:
        self.open_ts = ts
        self.n = 0
        self.trades = 0
        self.buy = self.sell = 0.0
        self.add_nb = self.add_na = self.pull_nb = self.pull_na = 0.0
        self.fill_b = self.fill_a = self.hidden = 0.0
        self.add_bb = self.add_ba = self.pull_bb = self.pull_ba = 0.0
        m = mid if mid is not None else math.nan
        self.mid_open = self.mid_high = self.mid_low = m
        self.mlofi1 = self.mlofi_near = 0.0
        self.sweep_ticks = 0.0
        self.term_filled = self.term_pulled = self.refills = 0.0


class InputAssembler:
    """Streaming assembly of the five inputs (one code path, past-only)."""

    def __init__(
        self,
        cfg: Config,
        calendar: EventCalendar | None = None,
        days_to_expiry: float = math.nan,
        days_since_roll: float = math.nan,
    ) -> None:
        ic = cfg.raw["inputs"]
        self.cfg = cfg
        self.tick_pts = float(cfg.raw["costs"]["tick_size_pts"])
        self.event_window = int(ic["event_window"])
        self.bar_events = int(ic["bar_events"])
        self.bar_max_ns = int(float(ic["bar_max_duration_s"]) * NS)
        self.bar_window = int(ic["bar_window"])
        self.tactical_steps = int(ic["tactical_steps"])
        self.slow_steps = int(ic["slow_steps"])
        self.slow_every = int(float(ic["slow_step_s"]))
        self.regime_windows = [int(w) for w in ic["regime_windows_s"]]
        self.calendar = calendar if calendar is not None else EventCalendar.load(cfg)
        self.clock = PhaseClock.from_config(cfg)
        self.days_to_expiry = days_to_expiry
        self.days_since_roll = days_since_roll
        self._depth_near = math.nan

        self.events: deque[list[float]] = deque(maxlen=self.event_window)
        self.bars: deque[list[float]] = deque(maxlen=self.bar_window)
        self.tactical: deque[list[float]] = deque(maxlen=self.tactical_steps)
        self.slow: deque[list[float]] = deque(maxlen=self.slow_steps)
        self.regime_pct = {
            b: {w: RollingPercentile(w * NS) for w in self.regime_windows}
            for b in REGIME_BASES
        }
        self._bar: _BarAcc | None = None
        self._prev_ts: int | None = None
        self._prev_mid: float | None = None
        self._cur_sec: int | None = None
        self._sec_delta = 0.0
        self._sec_vol = 0.0
        self._sec_events = 0
        self._last_out: dict | None = None
        self._sec_count = 0  # completed seconds (drives the 10 s cadence)
        self._slow_acc: list[list[float]] = []  # last N tactical rows for 10 s aggregation

        self.regime_names = self._regime_names()

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _nz(v: float, alt: float = 0.0) -> float:
        return v if v is not None and not math.isnan(v) else alt

    def _norm_vol(self, x: float, out: dict, seconds: float) -> float:
        v = out.get("v_scale", math.nan)
        if math.isnan(v):
            return math.nan
        return x / max(v * seconds, 1e-9)

    def _norm_dist_ticks(self, x_ticks: float, out: dict) -> float:
        s = out.get("sigma_dist", math.nan)
        if math.isnan(s):
            return math.nan
        return x_ticks * self.tick_pts / max(s, 1e-9)

    # ------------------------------------------------------------ the step

    def step(self, cols: dict, i: int, out: dict) -> None:
        """Ingest one flow row + its Phase 3/4 feature vector."""
        ts = int(cols["ts"][i])
        mid = out["mid_ticks"]
        mid = mid if not math.isnan(mid) else self._prev_mid

        # ---- input 1: event vector -------------------------------------
        dt_s = (ts - self._prev_ts) / NS if self._prev_ts is not None else 0.0
        t_buy = float(cols["t_buy"][i])
        t_sell = float(cols["t_sell"][i])
        tph, tpl = float(cols["t_px_high"][i]), float(cols["t_px_low"][i])
        if (t_buy + t_sell) > 0 and mid is not None:
            t_mid_ticks = ((tph + tpl) / 2) / (self.tick_pts / 1e-9)
            trade_dist = self._norm_dist_ticks(t_mid_ticks - mid, out)
        else:
            trade_dist = 0.0
        mid_chg = (
            self._norm_dist_ticks(mid - self._prev_mid, out)
            if (mid is not None and self._prev_mid is not None)
            else 0.0
        )
        ev = [
            dt_s,
            self._norm_vol(t_buy, out, 1.0),
            self._norm_vol(t_sell, out, 1.0),
            float(cols["t_buy_n"][i] + cols["t_sell_n"][i]),
            trade_dist,
            self._norm_vol(float(cols["add_near_b"][i]), out, 1.0),
            self._norm_vol(float(cols["add_near_a"][i]), out, 1.0),
            self._norm_vol(float(cols["pull_near_b"][i]), out, 1.0),
            self._norm_vol(float(cols["pull_near_a"][i]), out, 1.0),
            self._norm_vol(float(cols["fill_b"][i]), out, 1.0),
            self._norm_vol(float(cols["fill_a"][i]), out, 1.0),
            self._norm_vol(float(cols["hidden_b"][i] + cols["hidden_a"][i]), out, 1.0),
            mid_chg,
            out["spread_ticks"],
            out["book_imbalance_l1"],
            out["microprice_disp_ticks_norm"],
            out["mlofi_1_s_norm"],  # note: short-window sum at event time
            float(cols["age_best_b"][i]) / NS,
            float(cols["age_best_a"][i]) / NS,
            float(cols["term_filled"][i]),
            float(cols["term_pulled_touched"][i] + cols["term_pulled_untouched"][i]),
            float(cols["refill_b"][i] + cols["refill_a"][i]),
        ]
        self.events.append(ev)

        # ---- input 2: adaptive flow bars --------------------------------
        if self._bar is None:
            self._bar = _BarAcc(ts, mid)
        b = self._bar
        b.n += 1
        b.trades += int(cols["t_buy_n"][i] + cols["t_sell_n"][i])
        b.buy += t_buy
        b.sell += t_sell
        b.add_nb += float(cols["add_near_b"][i])
        b.add_na += float(cols["add_near_a"][i])
        b.pull_nb += float(cols["pull_near_b"][i])
        b.pull_na += float(cols["pull_near_a"][i])
        b.fill_b += float(cols["fill_b"][i])
        b.fill_a += float(cols["fill_a"][i])
        b.hidden += float(cols["hidden_b"][i] + cols["hidden_a"][i])
        b.add_bb += float(cols["add_best_b"][i])
        b.add_ba += float(cols["add_best_a"][i])
        b.pull_bb += float(cols["pull_best_b"][i])
        b.pull_ba += float(cols["pull_best_a"][i])
        if mid is not None:
            if math.isnan(b.mid_open):
                b.mid_open = b.mid_high = b.mid_low = mid
            b.mid_high = max(b.mid_high, mid)
            b.mid_low = min(b.mid_low, mid)
        b.mlofi1 += float(cols["flow_b_1"][i] - cols["flow_a_1"][i])
        for l in range(1, 4):
            b.mlofi_near += float(cols[f"flow_b_{l}"][i] - cols[f"flow_a_{l}"][i])
        if (t_buy > 0) != (t_sell > 0) and (t_buy + t_sell) > 0:
            b.sweep_ticks += (tph - tpl) / (self.tick_pts / 1e-9)
        b.term_filled += float(cols["term_filled"][i])
        b.term_pulled += float(
            cols["term_pulled_touched"][i] + cols["term_pulled_untouched"][i]
        )
        b.refills += float(cols["refill_b"][i] + cols["refill_a"][i])
        if b.n >= self.bar_events or (ts - b.open_ts) >= self.bar_max_ns:
            self._close_bar(ts, mid, out)

        # ---- inputs 3/4/5: the 1-second clock ----------------------------
        sec = ts // NS
        if self._cur_sec is None:
            self._cur_sec = sec
        while self._cur_sec < sec:
            self._close_second(out)
            self._cur_sec += 1
            if sec - self._cur_sec > self.tactical_steps:
                self._cur_sec = sec - self.tactical_steps  # gap-fill cap
        self._sec_delta += t_buy - t_sell
        self._sec_vol += t_buy + t_sell
        self._sec_events += 1
        self._prev_ts = ts
        self._prev_mid = mid if mid is not None else self._prev_mid
        self._last_out = out

    # ------------------------------------------------------------ closers

    def _close_bar(self, ts: int, mid: float | None, out: dict) -> None:
        b = self._bar
        dur = max((ts - b.open_ts) / NS, 1e-9)
        mid_close = mid if mid is not None else b.mid_open
        nv = lambda x, s=dur: self._norm_vol(x, out, s)  # noqa: E731
        nd = lambda t: self._norm_dist_ticks(t, out)  # noqa: E731
        d = out.get("d_scale", math.nan)
        row = [
            dur, float(b.n), float(b.trades),
            nv(b.buy), nv(b.sell), nv(b.buy - b.sell),
            nv(b.add_nb), nv(b.add_na), nv(b.pull_nb), nv(b.pull_na),
            nv(b.fill_b), nv(b.fill_a), nv(b.hidden),
            (b.add_bb / (b.add_bb + b.fill_b + b.pull_bb))
            if (b.add_bb + b.fill_b + b.pull_bb) > 0 else 0.5,
            (b.add_ba / (b.add_ba + b.fill_a + b.pull_ba))
            if (b.add_ba + b.fill_a + b.pull_ba) > 0 else 0.5,
            nd(mid_close - b.mid_open) if not math.isnan(b.mid_open) else 0.0,
            nd(b.mid_high - b.mid_low) if not math.isnan(b.mid_high) else 0.0,
            b.mlofi1 / d if not math.isnan(d) else math.nan,
            b.mlofi_near / d if not math.isnan(d) else math.nan,
            nd(b.sweep_ticks),
            b.term_filled, b.term_pulled, b.refills,
            b.n / dur,
            out["absorption_net_s"], out["book_imbalance_near"],
            out["spread_ticks"],
            out["queue_turnover_bid_m"], out["queue_turnover_ask_m"],
            out["microprice_disp_ticks_norm"],
        ]
        self.bars.append(row)
        self._bar = _BarAcc(ts, mid)

    def _close_second(self, out: dict) -> None:
        """Fold the completed second into the tactical ring, the 10 s slow
        cadence, and the regime percentile trackers."""
        o = self._last_out if self._last_out is not None else out
        delta_n = self._norm_vol(self._sec_delta, o, 1.0)
        vol_n = self._norm_vol(self._sec_vol, o, 1.0)
        cur_mid = self._prev_mid if self._prev_mid is not None else math.nan
        prev_mid = self.tactical[-1][-1] if self.tactical else math.nan
        mid_chg = (
            self._norm_dist_ticks(cur_mid - prev_mid, o)
            if not (math.isnan(cur_mid) or math.isnan(prev_mid))
            else 0.0
        )
        row = [
            delta_n, vol_n, float(self._sec_events),
            o["aggr_delta_s_norm"], o["absorption_bid_s"], o["absorption_ask_s"],
            o["mlofi_near_s_norm"], o["price_impact_m_norm"],
            o["sweep_buy_ticks_m_norm"], o["sweep_sell_ticks_m_norm"],
            o["sweep_failure_score"],
            o["queue_depletion_bid_s"], o["queue_depletion_ask_s"],
            o["liquidity_survival_ratio_l"], o["replenish_bid_m"], o["replenish_ask_m"],
            o["book_resiliency_bid"], o["book_resiliency_ask"],
            o["liquidity_vacuum_up"], o["liquidity_vacuum_down"],
            o["trade_burst_intensity_s"],
            mid_chg,
            o["spread_ticks"],
        ]
        # hidden trailing slot: the second's mid (for the next mid-change)
        self.tactical.append(row + [cur_mid])
        self._slow_acc.append(row + [cur_mid])

        # regime bases (pushed at 1 Hz, queried past-only at sample time)
        ts_ns = self._cur_sec * NS
        for base, val in self._regime_sample(o).items():
            if not math.isnan(val):
                for w in self.regime_windows:
                    self.regime_pct[base][w].push(ts_ns, val)

        self._sec_count += 1
        if self._sec_count % self.slow_every == 0 and self._slow_acc:
            self._close_slow(o)
            self._slow_acc = []

        self._sec_delta = 0.0
        self._sec_vol = 0.0
        self._sec_events = 0

    def _close_slow(self, o: dict) -> None:
        acc = self._slow_acc
        mids = [r[-1] for r in acc if not math.isnan(r[-1])]
        ret = self._norm_dist_ticks(mids[-1] - mids[0], o) if len(mids) >= 2 else 0.0
        rng = self._norm_dist_ticks(max(mids) - min(mids), o) if mids else 0.0
        di = sum(r[0] for r in acc if not math.isnan(r[0]))
        vi = sum(r[1] for r in acc if not math.isnan(r[1]))
        ev = sum(r[2] for r in acc)
        # "average absorption" (plan input 4): mean net over the step's
        # tactical rows (indices 4/5 = absorption bid/ask)
        abs_mean = sum(r[4] - r[5] for r in acc) / len(acc)
        d = o.get("d_scale", math.nan)
        depth_n = self._depth_near / d if not math.isnan(d) else math.nan
        row = [
            ret, rng, di, vi, ev,
            di,  # cvd_slope_norm == normalized delta increment over the step
            abs_mean, o["failed_sweeps_l"], o["mlofi_near_m_norm"],
            depth_n,
            o["spread_ticks"], o["trade_burst_intensity_s"],
            o["book_resiliency_bid"], o["book_resiliency_ask"],
            o["directional_efficiency_m"],
        ]
        self.slow.append(row)

    def note_depth(self, cols: dict, i: int) -> None:
        """Record near depth for the slow context (called by the driver
        alongside step(); kept separate so step() signature stays stable)."""
        if cols["valid"][i]:
            self._depth_near = float(
                cols["depth_b_near"][i] + cols["depth_a_near"][i]
            )

    # ------------------------------------------------------------ regime

    def _regime_sample(self, o: dict) -> dict[str, float]:
        return {
            "event_rate": float(self._sec_events),
            "buy_vol": (self._sec_vol + self._sec_delta) / 2,
            "sell_vol": (self._sec_vol - self._sec_delta) / 2,
            "delta": self._sec_delta,
            "volatility": o.get("sigma_30s", math.nan),
            "spread": o["spread_ticks"],
            "absorption": o["absorption_net_s"],
            "depth": self._depth_near,
            "queue_turnover": (o["queue_turnover_bid_m"] + o["queue_turnover_ask_m"]) / 2,
            "replenishment": (o["replenish_bid_m"] + o["replenish_ask_m"]) / 2,
            "sweep_activity": o["sweep_buy_ticks_m"] + o["sweep_sell_ticks_m"],
        }

    def _regime_names(self) -> list[str]:
        names = []
        for b in REGIME_BASES:
            for w in self.regime_windows:
                names.append(f"{b}_pct_{w}s")
        names += [
            "sigma_dist", "sigma_dist_pct", "v_scale", "v_scale_pct",
            "d_scale", "d_scale_pct", "norm_ready",
            "seconds_to_next_event", "seconds_since_last_event",
            "next_event_tier", "last_event_tier", "in_blackout",
            "phase_code", "is_settlement",
            "days_to_expiry", "days_since_roll",
        ]
        return names

    def regime_vector(self, ts: int, o: dict) -> np.ndarray:
        vals: list[float] = []
        cur = self._regime_sample(o)
        for b in REGIME_BASES:
            for w in self.regime_windows:
                v = cur[b]
                vals.append(
                    self.regime_pct[b][w].percentile(v) if not math.isnan(v) else math.nan
                )
        cal = self.calendar.features(ts)
        cap = 7 * 24 * 3600.0  # tensor-safe cap for open-ended horizons
        phase, settle = self.clock.classify(ts)
        vals += [
            o["sigma_dist"], o["sigma_dist_pct"], o["v_scale"], o["v_scale_pct"],
            o["d_scale"], o["d_scale_pct"], o["norm_ready"],
            min(cal["seconds_to_next_scheduled_event"], cap),
            min(cal["seconds_since_last_scheduled_event"], cap),
            cal["next_event_tier"], cal["last_event_tier"],
            float(self.calendar.in_blackout(ts)),
            float(PHASES.index(phase)), float(settle),
            self.days_to_expiry, self.days_since_roll,
        ]
        return np.asarray(vals, dtype=np.float32)

    # ------------------------------------------------------------ sampling

    @staticmethod
    def _pad(ring: deque, width: int, rows: int, drop_last: int = 0) -> tuple[np.ndarray, int]:
        arr = np.zeros((rows, width), dtype=np.float32)
        data = list(ring)
        if drop_last:
            data = [r[:-drop_last] for r in data]
        n = len(data)
        if n:
            arr[rows - n :] = np.asarray(data, dtype=np.float32)
        return arr, n

    def sample(self, ts: int | None = None) -> InputSample:
        """Assemble the five inputs from current (past-only) state."""
        o = self._last_out
        assert o is not None, "sample() before any step()"
        ts = ts if ts is not None else self._prev_ts
        ev, ev_n = self._pad(self.events, len(EVENT_FEATURES), self.event_window)
        ba, ba_n = self._pad(self.bars, len(BAR_FEATURES), self.bar_window)
        ta, ta_n = self._pad(
            self.tactical, len(TACTICAL_FEATURES), self.tactical_steps, drop_last=1
        )
        sl, sl_n = self._pad(self.slow, len(SLOW_FEATURES), self.slow_steps)
        return InputSample(
            ts=ts,
            events=ev, events_len=ev_n,
            bars=ba, bars_len=ba_n,
            tactical=ta, tactical_len=ta_n,
            slow=sl, slow_len=sl_n,
            regime=self.regime_vector(ts, o),
            regime_names=self.regime_names,
            norm_ready=bool(o["norm_ready"]),
        )


def assemble_session(
    date: dt.date,
    cfg: Config,
    sample_every_s: float = 60.0,
    cols: dict | None = None,
):
    """Historical driver: flow stream -> features -> assembler, sampling
    every `sample_every_s`. Same step() a live session would call."""
    from features.flow_stream import replay_session_flow

    if cols is None:
        fs = replay_session_flow(date, cfg)
        cols = fs.cols

    dte = dsr = math.nan
    try:
        from calendar_mod.roll_ledger import RollLedger

        led = RollLedger.load(cfg)
        a = led.active(date)
        dte = float(a.days_to_expiry)
        past_rolls = [r for r in led.roll_dates() if r <= date]
        dsr = float((date - past_rolls[-1]).days) if past_rolls else math.nan
    except (FileNotFoundError, KeyError):
        pass

    nfe = NormalizedFeatureEngine(cfg)
    asm = InputAssembler(cfg, days_to_expiry=dte, days_since_roll=dsr)
    samples: list[InputSample] = []
    every = int(sample_every_s * NS)
    next_at = None
    n = len(cols["ts"])
    for i in range(n):
        out = nfe.step(cols, i)
        asm.note_depth(cols, i)
        asm.step(cols, i, out)
        ts = int(cols["ts"][i])
        if next_at is None:
            next_at = ts + every
        elif ts >= next_at:
            samples.append(asm.sample(ts))
            next_at = ts + every
    return asm, samples
