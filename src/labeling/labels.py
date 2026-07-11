"""Phase 6/7 — Step 18: future labels on tradeable prices.

Convention (config [labels], stored with every label set):
  - entry at the AGGRESSIVE side at sample time t: long buys the ask,
    short sells the bid;
  - exits marked at the touch of the OPPOSITE side (plan default): a
    long's favorable path is the best-BID maximum over (t, t+h]; a
    short's favorable path is the best-ASK minimum;
  - explicit cost model in raw ticks (never normalized, Critical Rule 21);
  - dual units (v2.1): every movement label in points AND /sigma_h(t);
    time-to-extreme additionally as fraction of horizon (Phase 4A rule 4).

Direction classes (per horizon): BULLISH when the cost-adjusted favorable
long move exceeds `direction_ratio` x the cost-adjusted adverse move
(adverse + cost — the stop-out also pays the round trip); BEARISH mirrored;
both -> the larger net side; neither -> NO_TRADE, which therefore means
"not worth trading after costs", not "small move".

The label path uses per-second best-bid/ask extremes with carry-forward
through quiet seconds (the resting book persists between events); times to
extremes are second-resolution (documented).

Labels are training-time supervision with NO live counterpart, so this
module is vectorized numpy — the one-code-path rule binds shared
feature/trigger computation, not supervision (rationale in the Phase 6
report). Everything here reads only (t, t+h] paths and past-only sigma(t).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from utilities.config import Config

NS = 1_000_000_000

DIR_NO_TRADE = 0
DIR_BULLISH = 1
DIR_BEARISH = 2

LABEL_COLUMNS_PER_H = [
    "upside_pts", "downside_pts", "adverse_long_pts", "adverse_short_pts",
    "final_return_pts", "time_to_high_s", "time_to_low_s",
    "upside_norm", "downside_norm", "adverse_long_norm", "adverse_short_norm",
    "final_return_norm", "time_to_high_frac", "time_to_low_frac",
    "cost_norm", "sigma_pts", "direction", "window_complete",
]


@dataclass(frozen=True)
class SecondPath:
    """Per-second tradeable-price path for one session (carry-forward)."""

    sec0: int                 # first second (exchange feed time)
    bid_high: np.ndarray      # fixed-point price units (1e-9 pt)
    bid_low: np.ndarray
    ask_high: np.ndarray
    ask_low: np.ndarray
    mid_last: np.ndarray      # price units; NaN before the first valid book

    def __len__(self) -> int:
        return len(self.bid_high)


def build_second_path(cols: dict) -> SecondPath:
    """Fold the flow rows into per-second bid/ask extremes (price units)
    with carry-forward through seconds that have no valid rows."""
    ts = cols["ts"].astype(np.int64)
    valid = cols["valid"].astype(bool)
    bid = cols["bid_px"].astype(np.float64)
    ask = cols["ask_px"].astype(np.float64)
    sec = (ts // NS).astype(np.int64)
    sec0, sec1 = int(sec[0]), int(sec[-1])
    n = sec1 - sec0 + 1
    bh = np.full(n, np.nan)
    bl = np.full(n, np.nan)
    ah = np.full(n, np.nan)
    al = np.full(n, np.nan)
    ml = np.full(n, np.nan)
    idx = sec - sec0
    v = valid
    # fmax/fmin: NaN-initialized slots take the first real value
    # (np.maximum would propagate the NaN forever)
    np.fmax.at(bh, idx[v], bid[v])
    np.fmin.at(bl, idx[v], bid[v])
    np.fmax.at(ah, idx[v], ask[v])
    np.fmin.at(al, idx[v], ask[v])
    # last valid quotes per second (last write wins; rows are in order) —
    # the book that PERSISTS into quiet seconds is the closing quote, not
    # the second's extreme
    mid = (bid + ask) / 2.0
    b_last = np.full(n, np.nan)
    a_last = np.full(n, np.nan)
    ml[idx[v]] = mid[v]
    b_last[idx[v]] = bid[v]
    a_last[idx[v]] = ask[v]
    last_b = last_a = last_m = np.nan
    for i in range(n):
        if math.isnan(bh[i]):
            bh[i] = bl[i] = last_b
            ah[i] = al[i] = last_a
            ml[i] = last_m
        else:
            last_b, last_a, last_m = b_last[i], a_last[i], ml[i]
    return SecondPath(sec0, bh, bl, ah, al, ml)


class LabelEngine:
    def __init__(self, cfg: Config) -> None:
        lc = cfg.raw["labels"]
        self.tick_pts = float(cfg.raw["costs"]["tick_size_pts"])
        self.px_per_pt = 1e9  # fixed-point price units per point
        self.horizons = [int(h) for h in cfg.raw["normalization"]["sigma_horizons_s"]]
        self.cost_pts = float(lc["cost_round_trip_ticks"]) * self.tick_pts
        self.ratio = float(lc["direction_ratio"])
        self.convention = str(lc["convention"])

    def metadata(self) -> dict:
        """Stored with every label file (plan: convention + cost model)."""
        return {
            "convention": self.convention,
            "cost_round_trip_pts": self.cost_pts,
            "direction_ratio": self.ratio,
            "horizons_s": list(self.horizons),
            "path_resolution_s": 1,
            "units": "points + sigma_h-normalized twins (v2.1 dual-unit)",
        }

    def label_sample(
        self,
        path: SecondPath,
        t_ns: int,
        sigma_by_h: dict[int, float],
    ) -> dict[str, float]:
        """All label columns for one sample at t (entry state = the last
        quotes at or before t; window = the h seconds after t)."""
        out: dict[str, float] = {}
        s0 = int(t_ns // NS) - path.sec0
        # entry at the aggressive side, conservatively estimated from the
        # sample second: pay the second's WORST ask (long) / hit its worst
        # bid (short); quiet seconds carry the resting quotes unchanged
        entry_ask = path.ask_high[s0]
        entry_bid = path.bid_low[s0]
        mid0 = path.mid_last[s0]

        for h in self.horizons:
            p = f"h{h}_"
            lo = s0 + 1
            hi = s0 + h  # inclusive window (t, t+h]
            complete = hi < len(path)
            out[p + "window_complete"] = float(complete)
            if not complete or math.isnan(entry_ask) or math.isnan(entry_bid):
                for c in LABEL_COLUMNS_PER_H:
                    if c != "window_complete":
                        out[p + c] = math.nan
                out[p + "window_complete"] = 0.0
                continue
            w_bh = path.bid_high[lo : hi + 1]
            w_bl = path.bid_low[lo : hi + 1]
            w_ah = path.ask_high[lo : hi + 1]
            w_al = path.ask_low[lo : hi + 1]
            scale = self.px_per_pt
            up = (np.nanmax(w_bh) - entry_ask) / scale       # long -> exit at bid
            adv_l = (entry_ask - np.nanmin(w_bl)) / scale    # long adverse
            down = (entry_bid - np.nanmin(w_al)) / scale     # short -> cover at ask
            adv_s = (np.nanmax(w_ah) - entry_bid) / scale    # short adverse
            t_hi = float(np.nanargmax(w_bh) + 1)             # seconds after t
            t_lo = float(np.nanargmin(w_al) + 1)
            mid1 = path.mid_last[hi]
            fin = (
                (mid1 - mid0) / scale  # price units -> points
                if not (math.isnan(mid1) or math.isnan(mid0))
                else math.nan
            )

            sig = sigma_by_h.get(h, math.nan)
            has_sig = not math.isnan(sig) and sig > 0
            nz = lambda x: (x / sig) if has_sig else math.nan  # noqa: E731

            out[p + "upside_pts"] = up
            out[p + "downside_pts"] = down
            out[p + "adverse_long_pts"] = adv_l
            out[p + "adverse_short_pts"] = adv_s
            out[p + "final_return_pts"] = fin
            out[p + "time_to_high_s"] = t_hi
            out[p + "time_to_low_s"] = t_lo
            out[p + "upside_norm"] = nz(up)
            out[p + "downside_norm"] = nz(down)
            out[p + "adverse_long_norm"] = nz(adv_l)
            out[p + "adverse_short_norm"] = nz(adv_s)
            out[p + "final_return_norm"] = nz(fin)
            out[p + "time_to_high_frac"] = t_hi / h
            out[p + "time_to_low_frac"] = t_lo / h
            out[p + "cost_norm"] = nz(self.cost_pts)
            out[p + "sigma_pts"] = sig

            # direction class — evaluated in vol units (equivalently: the
            # ratio test is scale-free, but sigma must exist so thresholds
            # are era-comparable; cold samples carry NaN sigma and are
            # excluded upstream anyway)
            # adverse is floored at 0 in the decision (a gap that never
            # retraces earns no "negative risk" credit); the raw signed
            # adverse labels above stay unfloored
            fav_l = up - self.cost_pts
            fav_s = down - self.cost_pts
            net_adv_l = max(adv_l, 0.0) + self.cost_pts
            net_adv_s = max(adv_s, 0.0) + self.cost_pts
            bull = fav_l > 0 and fav_l >= self.ratio * net_adv_l
            bear = fav_s > 0 and fav_s >= self.ratio * net_adv_s
            if bull and bear:
                d = DIR_BULLISH if fav_l >= fav_s else DIR_BEARISH
            elif bull:
                d = DIR_BULLISH
            elif bear:
                d = DIR_BEARISH
            else:
                d = DIR_NO_TRADE
            out[p + "direction"] = float(d)
        return out
