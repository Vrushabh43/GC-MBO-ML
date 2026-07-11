"""Phase 6 — Step 19: the training-sample index.

One continuous feature timeline (the flow/feature stream) + a SEPARATE
sample index (plan Phase 6): ~1 sample/second while the market is active
and the scales are warm, plus event-triggered extras (sweep, absorption
onset, MLOFI shift, trade burst, queue collapse, vacuum, major
replenishment) under a minimum-spacing rule and per-type-per-session caps.

Sample SELECTION is past-only and live-reusable: `sample_trigger` is a
pure function of the current feature row (the same function a live
session calls). Label attachment and uniqueness weighting are
training-only artifacts computed after selection.

Overlap is first-class (plan Phase 6): per horizon, every sample gets a
López-de-Prado average-uniqueness weight (mean over its label window of
1/concurrency) and the report states the EFFECTIVE sample size Σw —
row count is never information count.

Hygiene columns per sample: release policy over the label window
(exclude/tag/ok + calendar coverage warning flag), session-boundary
containment (windows never span the maintenance break), norm_ready, and
`label_end_ts` for Phase 9 purging.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from calendar_mod.events import EventCalendar
from features.normalization import NS, NormalizedFeatureEngine
from labeling.labels import LabelEngine, build_second_path
from utilities.config import Config

TRIGGERS = (
    "clock", "sweep", "absorption_onset", "mlofi_shift", "trade_burst",
    "queue_collapse", "vacuum", "replenishment",
)


class TriggerState:
    """Past-only event-trigger evaluation (identical live)."""

    def __init__(self, cfg: Config) -> None:
        s = cfg.raw["samples"]
        self.thr_abs = float(s["trigger_absorption_net"])
        self.thr_mlofi = float(s["trigger_mlofi_norm"])
        self.thr_burst = float(s["trigger_burst"])
        self.thr_depl = float(s["trigger_depletion"])
        self.thr_vac = float(s["trigger_vacuum"])
        self.thr_repl = float(s["trigger_replenish"])
        self._prev_abs = 0.0
        self._prev_sweep = 0.0

    def evaluate(self, out: dict) -> str | None:
        """First matching event trigger for this row, else None."""
        trig = None
        a = out["absorption_net_s"]
        sweep_now = out["sweep_buy_ticks_m"] + out["sweep_sell_ticks_m"]
        if sweep_now > self._prev_sweep:
            trig = "sweep"
        elif abs(a) >= self.thr_abs and abs(self._prev_abs) < self.thr_abs:
            trig = "absorption_onset"
        elif not math.isnan(out["mlofi_near_s_norm"]) and abs(
            out["mlofi_near_s_norm"]
        ) >= self.thr_mlofi:
            trig = "mlofi_shift"
        elif out["trade_burst_intensity_s"] >= self.thr_burst:
            trig = "trade_burst"
        elif max(out["queue_depletion_bid_s"], out["queue_depletion_ask_s"]) >= self.thr_depl:
            trig = "queue_collapse"
        elif max(out["liquidity_vacuum_up"], out["liquidity_vacuum_down"]) >= self.thr_vac:
            trig = "vacuum"
        elif max(out["replenish_bid_m"], out["replenish_ask_m"]) >= self.thr_repl:
            trig = "replenishment"
        self._prev_abs = a
        self._prev_sweep = sweep_now
        return trig


def uniqueness_weights(
    starts_s: np.ndarray, horizon_s: int, span: tuple[int, int]
) -> np.ndarray:
    """Average-uniqueness weights (López de Prado): w_i = mean over the
    label window of 1/concurrency. Second resolution."""
    lo, hi = span
    n_sec = hi - lo + horizon_s + 2
    conc = np.zeros(n_sec, dtype=np.int64)
    a = starts_s - lo + 1          # window (t, t+h]
    for s in a:
        conc[s : s + horizon_s] += 1
    w = np.empty(len(starts_s))
    for i, s in enumerate(a):
        c = conc[s : s + horizon_s]
        w[i] = float(np.mean(1.0 / np.maximum(c, 1)))
    return w


def build_session_samples(
    date: dt.date,
    cfg: Config,
    cols: dict | None = None,
    calendar: EventCalendar | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Select samples, attach labels + hygiene + weights for one session.

    Returns (samples DataFrame, summary dict incl. effective-N per horizon).
    """
    from features.flow_stream import replay_session_flow

    if cols is None:
        cols = replay_session_flow(date, cfg).cols
    cal = calendar if calendar is not None else EventCalendar.load(cfg)
    s_cfg = cfg.raw["samples"]
    min_gap = int(float(s_cfg["min_spacing_s"]) * NS)
    cap = int(s_cfg["event_cap_per_type_per_session"])

    nfe = NormalizedFeatureEngine(cfg)
    trig = TriggerState(cfg)
    le = LabelEngine(cfg)

    # Selection evaluates at HALF-SECOND slots: the row that opens a new
    # second is the ~1 Hz clock candidate; the row that opens its second
    # half is the event-trigger candidate (the plan's event-triggered EXTRA
    # samples; min-spacing 0.5 s admits at most one extra per second).
    # compose() is a pure read, so evaluating at slots instead of every row
    # changes only the event-trigger evaluation instants (documented).
    picked: list[tuple[int, str, dict, dict]] = []  # (ts, trigger, sigma_by_h, features)
    counts: dict[str, int] = defaultdict(int)
    last_ts = -(10**18)
    last_slot = None
    session_ids: list = []
    half = NS // 2
    n = len(cols["ts"])
    for i in range(n):
        nfe.ingest(cols, i)
        ts = int(cols["ts"][i])
        slot = ts // half
        if slot == last_slot:
            continue
        last_slot = slot
        out = nfe.compose()
        active = out["norm_ready"] == 1.0
        kind: str | None = None
        if slot % 2 == 0:  # second boundary -> clock candidate
            if active:
                kind = "clock"
        else:  # mid-second -> event-trigger candidate
            k = trig.evaluate(out)
            if active and k is not None and counts[k] < cap:
                kind = k
        if kind is None or ts - last_ts < min_gap:
            continue
        counts[kind] += 1
        last_ts = ts
        picked.append(
            (
                ts,
                kind,
                {h: out[f"sigma_{h}s"] for h in le.horizons},
                out,  # Model A trains on the engineered vector at sample time
            )
        )
        session_ids.append(nfe._session)

    # ---- labels + hygiene (training-only, vectorized path) ---------------
    path = build_second_path(cols)
    max_h = max(le.horizons)
    rows: list[dict] = []
    for (ts, kind, sig, feats), sid in zip(picked, session_ids, strict=True):
        r: dict = {"ts": ts, "trigger": kind, "session": sid}
        # the feature vector AT the sample instant (past-only by
        # construction), prefixed to keep feature/label columns disjoint
        for k, v in feats.items():
            if k != "ts":
                r[f"f_{k}"] = v
        r.update(le.label_sample(path, ts, sig))
        end_ts = ts + max_h * NS
        r["label_end_ts"] = end_ts  # Phase 9 purging anchor
        r["release_policy"] = cal.label_window_policy(ts, end_ts)
        r["calendar_uncovered"] = float(cal.coverage_warning(ts, end_ts) is not None)
        # windows must not span the maintenance break: same session id at
        # both ends (the feature engine's 18:00-ET anchor)
        r["crosses_session"] = float(
            nfe._session_id(end_ts) != nfe._session_id(ts)
        )
        rows.append(r)

    df = pd.DataFrame(rows)
    summary: dict = {
        "date": str(date),
        "sessions": len(set(session_ids)),
        "picked": len(df),
        "by_trigger": dict(sorted(counts.items())),
        "label_metadata": le.metadata(),
    }
    # horizon keys ALWAYS present (a degraded/short session can pick zero
    # samples — the summary schema must not depend on that)
    for h in le.horizons:
        summary[f"h{h}"] = {
            "trainable": 0,
            "effective_n": 0.0,
            "class_balance": {"no_trade": math.nan, "bullish": math.nan,
                              "bearish": math.nan},
        }
    if len(df):
        # a sample is TRAINABLE for horizon h when its window is complete,
        # in-session, not release-excluded, and sigma existed
        starts = (df["ts"].to_numpy() // NS).astype(np.int64)
        span = (int(starts.min()), int(starts.max()))
        for h in le.horizons:
            ok = (
                (df[f"h{h}_window_complete"] == 1.0)
                & (df["crosses_session"] == 0.0)
                & (df["release_policy"] != "exclude")
                & (df[f"h{h}_sigma_pts"] > 0)  # normalized labels defined
            )
            df[f"h{h}_trainable"] = ok.astype(float)
            w = np.zeros(len(df))
            if ok.any():
                w[ok.to_numpy()] = uniqueness_weights(
                    starts[ok.to_numpy()], h, span
                )
            df[f"h{h}_uniqueness"] = w
            dcol = df.loc[ok, f"h{h}_direction"]
            summary[f"h{h}"] = {
                "trainable": int(ok.sum()),
                "effective_n": float(w.sum()),
                "class_balance": {
                    "no_trade": float((dcol == 0).mean()) if len(dcol) else math.nan,
                    "bullish": float((dcol == 1).mean()) if len(dcol) else math.nan,
                    "bearish": float((dcol == 2).mean()) if len(dcol) else math.nan,
                },
            }
    return df, summary


def write_session_samples(date: dt.date, cfg: Config, **kw):
    df, summary = build_session_samples(date, cfg, **kw)
    out_dir = cfg.raw["samples"]["sample_index_dir"]
    from utilities.config import REPO_ROOT

    d = REPO_ROOT / out_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"samples-{date.strftime('%Y%m%d')}.parquet"
    df.to_parquet(p, index=False)
    return p, df, summary
