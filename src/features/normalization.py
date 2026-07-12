"""Phase 4/4A — the canonical normalization architecture.

Three rolling scales (plan Phase 4A, the single authoritative spec):

    sigma_h(t) — median absolute h-horizon mid move (POINTS) over the
                 trailing scale window, h in config sigma_horizons_s
                 (30s/2m/10m per Phase 7). Normalizes tick/point distances
                 (features here; labels in Phase 7).
    v_scale(t) — median contracts-per-second over the trailing window.
                 Normalizes contract-denominated flow.
    d_scale(t) — median combined near-book depth (contracts) over the
                 trailing window. Normalizes book-depth quantities.

All three are strictly past-only, robust (median), updated on a 1-second
clock, computed identically historical/live (same streaming objects), and
reset at session boundaries — the 18:00-ET trading-day anchor, because
every daily file contains the 17:00-18:00 ET maintenance break — and at
contract rolls (per-session processing + the Step 12.5 `crosses_roll`
guard make a mid-stream roll impossible today).

`NormalizedFeatureEngine` wraps the Phase 3 `FeatureEngine` and emits, per
step: all raw features, the `_norm` twins (NaN during warm-up, with an
explicit `norm_ready` flag for the Phase 11 warm-up gate), the scales
themselves (raw + past-only session percentile — the scale level is
regime information), and the Phase 4 rolling-percentile features
(config [normalization.percentiles]).

Do-not-normalize (plan 4A): bounded/dimensionless scores, spread (its
percentile IS emitted), categoricals, probabilities — passed through
untouched, byte-identical to Phase 3 output.

DOCUMENTED DECISIONS (Phase 4 report):
- feature distances are normalized by sigma of config
  distance_sigma_horizon_s (default 2m); labels will use their own h.
- d_scale uses the near+mid depth bands (levels 1-6) — the flow stream
  aggregates bands, and the plan's top-5 default is not band-decomposable;
  "top-N combined" remains the configurable intent.
- v_scale counts zero-volume seconds (they are real market state); the
  division floor `SCALE_EPS` only guards the arithmetic.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from collections import deque
from zoneinfo import ZoneInfo

from features.core_features import FeatureEngine
from utilities.config import Config

NS = 1_000_000_000
SCALE_EPS = 1e-9

# ---- twin maps (explicit; everything else is do-not-normalize) -------------

# tick-denominated distances -> / sigma(distance horizon)   [plan 4A rule 1]
SIGMA_TWINS = (
    "price_progress_ticks_s",
    "price_progress_ticks_m",
    "microprice_disp_ticks",
    "sweep_buy_ticks_m",
    "sweep_sell_ticks_m",
    "sweep_net_ticks_m",  # Step 20 iteration signed set
)
# contract-denominated flow -> / (v_scale * window_seconds) [plan 4A rule 2]
V_TWINS = (
    ("aggr_delta_s", "window_short_s"),
    ("aggr_delta_m", "window_mid_s"),
    ("aggr_delta_l", "window_long_s"),
)
# depth-denominated flow -> / d_scale                        [plan 4A rule 2]
D_TWINS = (
    "mlofi_1_s",
    "mlofi_near_s",
    "mlofi_middle_s",
    "mlofi_deep_s",
    "mlofi_near_m",
    "mlofi_middle_m",
    "mlofi_deep_m",
)


class RollingMedian:
    """Median of the last `maxlen` pushed samples (1 sample/second => a
    count window IS the time window). Past-only by construction."""

    __slots__ = ("maxlen", "q", "sorted")

    def __init__(self, maxlen: int) -> None:
        self.maxlen = maxlen
        self.q: deque[float] = deque()
        self.sorted: list[float] = []

    def push(self, v: float) -> None:
        self.q.append(v)
        bisect.insort(self.sorted, v)
        if len(self.q) > self.maxlen:
            old = self.q.popleft()
            del self.sorted[bisect.bisect_left(self.sorted, old)]

    def median(self) -> float | None:
        s = self.sorted
        n = len(s)
        if n == 0:
            return None
        m = n // 2
        return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])

    def __len__(self) -> int:
        return len(self.q)

    def clear(self) -> None:
        self.q.clear()
        self.sorted.clear()


class RollingPercentile:
    """Past-only rank of a value among the trailing-window samples.
    Convention: query BEFORE pushing the current value."""

    __slots__ = ("horizon_ns", "q", "sorted")

    def __init__(self, horizon_ns: int) -> None:
        self.horizon_ns = horizon_ns
        self.q: deque[tuple[int, float]] = deque()
        self.sorted: list[float] = []

    def percentile(self, v: float) -> float:
        n = len(self.sorted)
        if n == 0:
            return float("nan")
        lo = bisect.bisect_left(self.sorted, v)
        hi = bisect.bisect_right(self.sorted, v)
        return (lo + hi) / 2 / n

    def push(self, ts: int, v: float) -> None:
        self.q.append((ts, v))
        bisect.insort(self.sorted, v)
        cut = ts - self.horizon_ns
        while self.q and self.q[0][0] <= cut:
            _, old = self.q.popleft()
            del self.sorted[bisect.bisect_left(self.sorted, old)]

    def clear(self) -> None:
        self.q.clear()
        self.sorted.clear()


class RollingRobustZ:
    """Streaming robust z-score: (x − median) / MAD over the trailing
    window. Streaming approximation (documented): deviations are taken
    against the median AT PUSH TIME — the exact windowed MAD would require
    re-scanning the window on every step. Past-only: query before push."""

    __slots__ = ("horizon_ns", "med", "dev")

    def __init__(self, horizon_ns: int) -> None:
        self.horizon_ns = horizon_ns
        self.med = RollingPercentile(horizon_ns)
        self.dev = RollingPercentile(horizon_ns)

    def _median(self, p: RollingPercentile) -> float | None:
        s = p.sorted
        n = len(s)
        if n == 0:
            return None
        m = n // 2
        return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])

    def zscore(self, v: float) -> float:
        m = self._median(self.med)
        d = self._median(self.dev)
        if m is None or d is None:
            return float("nan")
        return (v - m) / max(d, SCALE_EPS)

    def push(self, ts: int, v: float) -> None:
        m = self._median(self.med)
        self.med.push(ts, v)
        if m is not None:
            self.dev.push(ts, abs(v - m))

    def clear(self) -> None:
        self.med.clear()
        self.dev.clear()


class ScaleEngine:
    """The three canonical scales on a 1-second past-only clock."""

    def __init__(self, cfg: Config) -> None:
        n = cfg.raw["normalization"]
        self.window = int(n["scale_window_s"])
        self.horizons = [int(h) for h in n["sigma_horizons_s"]]
        self.dist_h = int(n["distance_sigma_horizon_s"])
        assert self.dist_h in self.horizons
        self.min_samples = int(n["min_samples"])
        self.tick_pts = float(cfg.raw["costs"]["tick_size_pts"])
        self.reset()

    def reset(self) -> None:
        self.cur_sec: int | None = None
        self.vol_acc = 0.0
        self.last_mid: float | None = None  # ticks
        self.last_depth: float | None = None
        self.mids: deque[float] = deque(maxlen=max(self.horizons) + 1)
        self.med_sigma = {h: RollingMedian(self.window) for h in self.horizons}
        self.med_v = RollingMedian(self.window)
        self.med_d = RollingMedian(self.window)

    def _close_second(self) -> None:
        """Fold the completed second into every scale (one sample each)."""
        self.med_v.push(self.vol_acc)
        self.vol_acc = 0.0
        if self.last_depth is not None:
            self.med_d.push(self.last_depth)
        if self.last_mid is not None:
            self.mids.append(self.last_mid)
            for h in self.horizons:
                if len(self.mids) > h:
                    move_pts = abs(self.mids[-1] - self.mids[-1 - h]) * self.tick_pts
                    self.med_sigma[h].push(move_pts)

    def step(self, ts: int, mid_ticks: float | None, group_vol: float,
             depth: float | None) -> None:
        sec = ts // NS
        if self.cur_sec is None:
            self.cur_sec = sec
        while self.cur_sec < sec:
            self._close_second()
            self.cur_sec += 1
            # long intra-session quiet gaps: cap the zero-fill at one full
            # window (anything older cannot influence the medians anyway)
            if sec - self.cur_sec > self.window:
                self.cur_sec = sec - self.window
        self.vol_acc += group_vol
        if mid_ticks is not None and not math.isnan(mid_ticks):
            self.last_mid = mid_ticks
        if depth is not None:
            self.last_depth = depth

    # -- values (None while warming up) ------------------------------------

    def sigma(self, h: int) -> float | None:
        m = self.med_sigma[h]
        return m.median() if len(m) >= self.min_samples else None

    def sigma_dist(self) -> float | None:
        return self.sigma(self.dist_h)

    def v_scale(self) -> float | None:
        return self.med_v.median() if len(self.med_v) >= self.min_samples else None

    def d_scale(self) -> float | None:
        return self.med_d.median() if len(self.med_d) >= self.min_samples else None


class NormalizedFeatureEngine:
    """Phase 3 features + Phase 4/4A normalization, one streaming step()."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.scales = ScaleEngine(cfg)
        # composite scores use NORMALIZED ingredients (plan 4A rule 3):
        # absorption's stall gets |Δmid|/sigma once the scale is warm
        self.fe = FeatureEngine(cfg, sigma_provider=self.scales.sigma_dist)
        n = cfg.raw["normalization"]
        self.tick_pts = float(cfg.raw["costs"]["tick_size_pts"])
        self.win_s = {
            "window_short_s": float(cfg.features.window_short_s),
            "window_mid_s": float(cfg.features.window_mid_s),
            "window_long_s": float(cfg.features.window_long_s),
        }
        self.tz = ZoneInfo(cfg.raw["calendar"]["tz"])
        self.pct_spec = {k: int(v) for k, v in n.get("percentiles", {}).items()}
        # robust z-scores (plan Phase 4): config list; default empty because
        # the 4A scale twins already cover volumes/distances — z-scores are
        # for quantities with no canonical scale (regime vector, Phase 5)
        self.z_spec = {k: int(v) for k, v in n.get("robust_z", {}).items()}
        self._session: dt.date | None = None
        self._sid_sec: int | None = None  # per-second session-id cache
        self._pct_sec: int | None = None  # internal 1-Hz percentile clock
        self._make_percentiles()
        self.names = None  # populated on first step

    def _make_percentiles(self) -> None:
        self.pct = {k: RollingPercentile(w * NS) for k, w in self.pct_spec.items()}
        self.zsc = {k: RollingRobustZ(w * NS) for k, w in self.z_spec.items()}
        # scale-level percentiles over the trailing session (past-only)
        self.scale_pct = {
            k: RollingPercentile(24 * 3600 * NS)
            for k in ("sigma_dist", "v_scale", "d_scale")
        }

    def _session_id(self, ts: int) -> dt.date:
        """18:00-ET-anchored trading day (maintenance break = boundary)."""
        t = dt.datetime.fromtimestamp(ts / NS, tz=dt.timezone.utc).astimezone(self.tz)
        return (t - dt.timedelta(hours=18)).date()

    # step() = ingest() + compose(): live and historical share step();
    # batch drivers ingest per row and compose only at sample instants.
    # Percentile/robust-z trackers are pushed on an INTERNAL 1-second clock
    # (a snapshot of the state as of the previous row when a second
    # completes) so their contents are independent of compose cadence —
    # the train/serve parity requirement for these features.

    def step(self, cols: dict, i: int) -> dict[str, float]:
        self.ingest(cols, i)
        return self.compose()

    def ingest(self, cols: dict, i: int) -> None:
        ts = int(cols["ts"][i])
        sec = ts // NS
        if sec != self._sid_sec:
            self._sid_sec = sec
            self._sid_cached = self._session_id(ts)
        sid = self._sid_cached
        if sid != self._session:
            # session boundary (incl. the intra-file maintenance break):
            # ALL rolling normalization state resets (plan Phase 4)
            self._session = sid
            self.scales.reset()
            self._make_percentiles()
            self._pct_sec = sec
        elif self._pct_sec is None:
            self._pct_sec = sec
        elif sec != self._pct_sec:
            # a second completed inside the session: snapshot the state as
            # of the previous row and feed the percentile/z trackers
            self._push_percentiles(self.compose())
            self._pct_sec = sec

        self.fe.ingest(cols, i)

        depth = None
        if cols["valid"][i]:
            depth = float(
                cols["depth_b_near"][i] + cols["depth_b_mid"][i]
                + cols["depth_a_near"][i] + cols["depth_a_mid"][i]
            )
        self.scales.step(
            ts,
            self.fe._last_mid,
            float(cols["t_buy"][i] + cols["t_sell"][i]),
            depth,
        )

    def _push_percentiles(self, snap: dict[str, float]) -> None:
        ts = int(snap["ts"])
        for key in ("sigma_dist", "v_scale", "d_scale"):
            v = snap[key]
            if not math.isnan(v):
                self.scale_pct[key].push(ts, v)
        for name, tracker in self.pct.items():
            v = snap[name]
            if not math.isnan(v):
                tracker.push(ts, v)
        for name, z in self.zsc.items():
            v = snap[name]
            if not math.isnan(v):
                z.push(ts, v)

    def compose(self) -> dict[str, float]:
        """Normalized feature vector from current state (pure read)."""
        out = self.fe.compose()

        sig = self.scales.sigma_dist()
        v = self.scales.v_scale()
        d = self.scales.d_scale()
        nan = float("nan")

        # rule 1: tick distances -> sigma twins (points / sigma_pts)
        for name in SIGMA_TWINS:
            out[f"{name}_norm"] = (
                out[name] * self.tick_pts / max(sig, SCALE_EPS) if sig is not None else nan
            )
        # rule 2a: flow volumes -> multiples of typical window volume
        for name, wkey in V_TWINS:
            out[f"{name}_norm"] = (
                out[name] / max(v * self.win_s[wkey], SCALE_EPS) if v is not None else nan
            )
        # rule 2b: depth flow -> multiples of standing depth scale
        for name in D_TWINS:
            out[f"{name}_norm"] = (
                out[name] / max(d, SCALE_EPS) if d is not None else nan
            )
        # dimensionful ratio rebuilt from normalized ingredients (rule 3):
        # impact = distance per volume -> (pts/sigma) per (vol/(v*W))
        if sig is not None and v is not None:
            w = self.win_s["window_mid_s"]
            out["price_impact_m_norm"] = (
                out["price_impact_m"] * self.tick_pts / max(sig, SCALE_EPS)
            ) * max(v * w, SCALE_EPS)
        else:
            out["price_impact_m_norm"] = nan

        # the scales themselves (regime information): raw + past-only pct
        # (trackers are fed by the internal 1-second clock, never here)
        for key, val in (("sigma_dist", sig), ("v_scale", v), ("d_scale", d)):
            out[key] = val if val is not None else nan
            out[f"{key}_pct"] = (
                self.scale_pct[key].percentile(val) if val is not None else nan
            )
        for h in self.scales.horizons:
            s = self.scales.sigma(h)
            out[f"sigma_{h}s"] = s if s is not None else nan
        out["norm_ready"] = float(
            sig is not None and v is not None and d is not None
        )

        # Phase 4 rolling-percentile / robust-z features (queries only;
        # trackers are fed on the internal 1-second clock in ingest)
        for name, tracker in self.pct.items():
            out[f"{name}_pctile"] = tracker.percentile(out[name])
        for name, z in self.zsc.items():
            out[f"{name}_rz"] = z.zscore(out[name])

        if self.names is None:
            self.names = list(out.keys())
        return out

    def run(self, cols: dict, sample_every_ns: int = 0):
        """Historical driver: ingest every row through the same code the
        live path uses; compose (pure read) only at kept instants."""
        n = len(cols["ts"])
        kept: list[dict[str, float]] = []
        next_keep = 0
        for i in range(n):
            self.ingest(cols, i)
            ts = int(cols["ts"][i])
            if sample_every_ns == 0 or ts >= next_keep or i == n - 1:
                kept.append(self.compose())
                if sample_every_ns:
                    next_keep = ts + sample_every_ns
        return self.names, kept

    def scale_config_fingerprint(self) -> dict:
        """Serialized with any model bundle (plan 4A verification list)."""
        n = self.cfg.raw["normalization"]
        return {
            "scale_window_s": n["scale_window_s"],
            "sigma_horizons_s": list(n["sigma_horizons_s"]),
            "distance_sigma_horizon_s": n["distance_sigma_horizon_s"],
            "sample_period_s": n["sample_period_s"],
            "min_samples": n["min_samples"],
            "d_scale_bands": "near+mid (levels 1-6)",
            "percentiles": dict(self.pct_spec),
        }
