"""Phase 3 — core order-flow features (plan Phase 3, build order Steps 7-12).

Input: the per-matching-event-group flow-primitive rows emitted by the
compiled core (gc_core flow recorder) for one tracked contract. Output: a
fixed-order vector of RAW feature values per step — the era-normalized
`_norm` twins are attached by Phase 4A, never here. Composite scores are
built from dimensionless ingredients (ratios of like quantities), so they
are bounded and scale-free by construction (plan do-not-normalize list).

ONE CODE PATH (Critical Rule 3): `FeatureEngine.step(cols, i)` is the only
computation route. Historical replay passes the drained column arrays and a
row index; live passes the same columns of length 1. There is no vectorized
variant.

Window suffixes: `_s` / `_m` / `_l` = the [features] window_short_s /
window_mid_s / window_long_s config values (defaults 2 s / 10 s / 60 s).

Sign conventions:
  - buy/bid-supportive pressure is positive where a feature is signed
    (aggr_delta*, mlofi*, book_imbalance*, absorption_net, stacking_net,
    microprice_disp_ticks: micro above mid = buy pressure = positive; the
    whole Step 20-iteration signed-asymmetry set: flow/fill/hidden/pull
    imbalances, side tilts, signed sweep features).
  - bounded scores live in [0, 1] (or [-1, 1] for signed ratios); ratio
    features with an empty window emit their neutral value (documented per
    formula) so downstream code never sees NaN.

Neutral naming (plan Phase 2/3): mechanics only — pulling, stacking,
cancel-before-touch, refills. Behavior is never labeled as intent.
"""
from __future__ import annotations

from utilities.config import Config

from features.windows import WindowMean, WindowMin, WindowPast, WindowSum

NS = 1_000_000_000


def _ratio(num: float, den: float, neutral: float = 0.0) -> float:
    """num/den with a defined neutral value for an empty denominator."""
    return num / den if den > 0 else neutral


class FeatureEngine:
    """Streaming composition of the Phase 3 feature set.

    `sigma_provider` (Phase 4A hook): a callable returning the current
    past-only sigma scale in POINTS, or None while warming up. When given,
    composite scores use sigma-NORMALIZED distance ingredients (plan 4A
    rule 3 — composites from normalized ingredients); without it (or during
    warm-up) raw tick distances are the documented fallback.
    """

    def __init__(self, cfg: Config, sigma_provider=None) -> None:
        ft = cfg.features
        self.sigma_provider = sigma_provider
        self.tick_pts = float(cfg.raw["costs"]["tick_size_pts"])
        self.tick = float(cfg.raw["costs"]["tick_size_pts"]) / 1e-9
        self.s = int(ft.window_short_s * NS)
        self.m = int(ft.window_mid_s * NS)
        self.l = int(ft.window_long_s * NS)
        self.sweep_min_ticks = ft.sweep_min_ticks
        self.reclaim_ns = int(ft.sweep_reclaim_s * NS)
        self.failed_retrace = ft.sweep_failed_retrace
        self.iceberg_k = ft.iceberg_softening

        # --- shared rolling state ------------------------------------------
        w = lambda h: WindowSum(h)  # noqa: E731
        # trades (T-only rule, Phase 1)
        self.t_buy_s, self.t_sell_s = w(self.s), w(self.s)
        self.t_buy_m, self.t_sell_m = w(self.m), w(self.m)
        self.t_buy_l, self.t_sell_l = w(self.l), w(self.l)
        self.t_n_s, self.t_n_l = w(self.s), w(self.l)
        # mid-price history
        self.mid_past_s = WindowPast(self.s)
        self.mid_past_m = WindowPast(self.m)
        self.path_m = w(self.m)  # sum |mid change| (ticks)
        # add/pull/fill flow at & near the touch, per side
        self.add_best_b_s, self.add_best_a_s = w(self.s), w(self.s)
        self.pull_best_b_s, self.pull_best_a_s = w(self.s), w(self.s)
        self.fill_b_s, self.fill_a_s = w(self.s), w(self.s)
        self.add_best_b_m, self.add_best_a_m = w(self.m), w(self.m)
        self.pull_best_b_m, self.pull_best_a_m = w(self.m), w(self.m)
        self.fill_b_m, self.fill_a_m = w(self.m), w(self.m)
        self.add_near_b_m, self.add_near_a_m = w(self.m), w(self.m)
        self.pull_near_b_m, self.pull_near_a_m = w(self.m), w(self.m)
        # MLOFI band sums (near = levels 1-3, middle = 4-6, deep = 7-10)
        self.mlofi_1_s = w(self.s)
        self.mlofi_near_s, self.mlofi_near_m = w(self.s), w(self.m)
        self.mlofi_middle_s, self.mlofi_middle_m = w(self.s), w(self.m)
        self.mlofi_deep_s, self.mlofi_deep_m = w(self.s), w(self.m)
        # depth baselines (resiliency / vacuum)
        self.depth_b_min_m, self.depth_a_min_m = WindowMin(self.m), WindowMin(self.m)
        self.depth_b_mean_l, self.depth_a_mean_l = WindowMean(self.l), WindowMean(self.l)
        # lifecycle terminations (survival / lifetime)
        self.term_filled_l = w(self.l)
        self.term_pulled_touched_l = w(self.l)
        self.term_pulled_untouched_l = w(self.l)
        self.life_all_sum_l = w(self.l)
        self.life_unchained_sum_l = w(self.l)
        self.term_unchained_l = w(self.l)
        # iceberg ingredients
        self.refill_b_l, self.refill_a_l = w(self.l), w(self.l)
        self.refill_conf_l = w(self.l)
        self.hidden_b_l, self.hidden_a_l = w(self.l), w(self.l)
        self.fill_b_l, self.fill_a_l = w(self.l), w(self.l)
        # sweeps
        self.sweep_buy_m, self.sweep_sell_m = w(self.m), w(self.m)
        self.failed_sweeps_l = w(self.l)  # pushes are SIGNED sweep dirs (±1)
        self._sweep: dict | None = None  # active sweep state
        self._last_mid: float | None = None  # ticks
        # signed-asymmetry set (Step 20 gate iteration): absolute depth-flow
        # magnitudes, denominators of the bounded flow-imbalance ratios
        self.absflow_1_s = w(self.s)
        self.absflow_near_s, self.absflow_near_m = w(self.s), w(self.m)

        self.names: list[str] = [
            # reference
            "ts", "mid_ticks", "spread_ticks",
            # Step 7 — aggressive delta (T-only)
            "aggr_delta_s", "aggr_delta_m", "aggr_delta_l",
            "aggr_delta_ratio_s", "aggr_delta_ratio_m", "aggr_delta_ratio_l",
            # Step 8 — replenishment
            "replenish_bid_m", "replenish_ask_m",
            # Step 9 — price progress
            "price_progress_ticks_s", "price_progress_ticks_m",
            "directional_efficiency_m",
            # Step 10 — absorption
            "absorption_bid_s", "absorption_ask_s", "absorption_net_s",
            # Step 12 — remaining features
            "stacking_bid_m", "stacking_ask_m", "stacking_net_m",
            "price_impact_m",
            "mlofi_1_s", "mlofi_near_s", "mlofi_middle_s", "mlofi_deep_s",
            "mlofi_near_m", "mlofi_middle_m", "mlofi_deep_m",
            "book_imbalance_l1", "book_imbalance_near", "book_imbalance_total",
            "microprice_disp_ticks",
            "sweep_buy_ticks_m", "sweep_sell_ticks_m",
            "sweep_failure_score", "failed_sweeps_l",
            "queue_depletion_bid_s", "queue_depletion_ask_s",
            "liquidity_survival_ratio_l", "cancel_before_touch_rate_l",
            "iceberg_score_bid_l", "iceberg_score_ask_l",
            "liquidity_vacuum_up", "liquidity_vacuum_down",
            "book_resiliency_bid", "book_resiliency_ask",
            "trade_burst_intensity_s",
            "queue_turnover_bid_m", "queue_turnover_ask_m",
            "order_lifetime_ms_l", "order_lifetime_chain_adj_ms_l",
            "liquidity_age_bid_s", "liquidity_age_ask_s", "liquidity_age_imbalance",
            # Step 20 gate iteration — signed-asymmetry set (positive =
            # buy/bid-supportive; bounded except sweep_net_ticks_m)
            "flow_imbalance_1_s", "flow_imbalance_near_s", "flow_imbalance_near_m",
            "fill_imbalance_s", "fill_imbalance_m", "hidden_fill_imbalance_l",
            "iceberg_asym_l", "pull_imbalance_m",
            "vacuum_tilt", "resiliency_tilt", "depletion_tilt_s",
            "replenish_tilt_m", "turnover_tilt_m",
            "book_imbalance_outer", "imbalance_tilt", "depth_concentration_tilt",
            "sweep_net_ticks_m", "sweep_failure_signed", "failed_sweep_net_ratio_l",
        ]

    # -- the single step ------------------------------------------------------
    #
    # step() = ingest() + compose(). Live and historical both go through
    # step(); batch drivers may call ingest() per row and compose() only at
    # sample instants — compose is a PURE read of ingested state, so the
    # result at any instant is identical either way (tested).

    def step(self, cols: dict, i: int) -> dict[str, float]:
        """Consume one flow-primitive row; return the current feature vector.

        `cols` maps column name -> array-like; `i` is the row index. Live
        processing passes length-1 columns with i=0 — same code, same path.
        """
        self.ingest(cols, i)
        return self.compose()

    def ingest(self, cols: dict, i: int) -> None:
        """Update all rolling state for one row (no output)."""
        ts = int(cols["ts"][i])
        tick = self.tick

        # ---- ingest: trades -------------------------------------------------
        t_buy = float(cols["t_buy"][i])
        t_sell = float(cols["t_sell"][i])
        for wsum, v in (
            (self.t_buy_s, t_buy), (self.t_buy_m, t_buy), (self.t_buy_l, t_buy),
            (self.t_sell_s, t_sell), (self.t_sell_m, t_sell), (self.t_sell_l, t_sell),
        ):
            wsum.push(ts, v)
        n_trades = float(cols["t_buy_n"][i] + cols["t_sell_n"][i])
        self.t_n_s.push(ts, n_trades)
        self.t_n_l.push(ts, n_trades)

        # ---- ingest: book state ---------------------------------------------
        valid = bool(cols["valid"][i])
        bid_px, ask_px = float(cols["bid_px"][i]), float(cols["ask_px"][i])
        bid_sz, ask_sz = float(cols["bid_sz"][i]), float(cols["ask_sz"][i])
        pre_mid = self._last_mid  # mid BEFORE this group (sweep reference)
        if valid:
            new_mid = (bid_px + ask_px) / 2.0 / tick  # in ticks
            if self._last_mid is not None:
                self.path_m.push(ts, abs(new_mid - self._last_mid))
            self._last_mid = new_mid
        mid = self._last_mid
        spread = ((ask_px - bid_px) / tick) if valid else float("nan")
        if mid is not None:
            self.mid_past_s.push(ts, mid)
            self.mid_past_m.push(ts, mid)

        d_b_near = float(cols["depth_b_near"][i])
        d_a_near = float(cols["depth_a_near"][i])
        d_b_tot = d_b_near + float(cols["depth_b_mid"][i]) + float(cols["depth_b_deep"][i])
        d_a_tot = d_a_near + float(cols["depth_a_mid"][i]) + float(cols["depth_a_deep"][i])
        self.depth_b_min_m.push(ts, d_b_near)
        self.depth_a_min_m.push(ts, d_a_near)
        self.depth_b_mean_l.push(ts, d_b_near)
        self.depth_a_mean_l.push(ts, d_a_near)

        # ---- ingest: add/pull/fill flow -------------------------------------
        for name, sinks in (
            ("add_best_b", (self.add_best_b_s, self.add_best_b_m)),
            ("add_best_a", (self.add_best_a_s, self.add_best_a_m)),
            ("pull_best_b", (self.pull_best_b_s, self.pull_best_b_m)),
            ("pull_best_a", (self.pull_best_a_s, self.pull_best_a_m)),
            ("fill_b", (self.fill_b_s, self.fill_b_m, self.fill_b_l)),
            ("fill_a", (self.fill_a_s, self.fill_a_m, self.fill_a_l)),
            ("add_near_b", (self.add_near_b_m,)),
            ("add_near_a", (self.add_near_a_m,)),
            ("pull_near_b", (self.pull_near_b_m,)),
            ("pull_near_a", (self.pull_near_a_m,)),
            ("hidden_b", (self.hidden_b_l,)),
            ("hidden_a", (self.hidden_a_l,)),
        ):
            v = float(cols[name][i])
            for sk in sinks:
                sk.push(ts, v)

        # ---- ingest: MLOFI ingredients (bid flow − ask flow per band) -------
        near = mid_band = deep = 0.0
        near_abs = 0.0
        for l in range(1, 4):
            fb = float(cols[f"flow_b_{l}"][i])
            fa = float(cols[f"flow_a_{l}"][i])
            near += fb - fa
            near_abs += abs(fb) + abs(fa)
        for l in range(4, 7):
            mid_band += float(cols[f"flow_b_{l}"][i]) - float(cols[f"flow_a_{l}"][i])
        for l in range(7, 11):
            deep += float(cols[f"flow_b_{l}"][i]) - float(cols[f"flow_a_{l}"][i])
        fb1 = float(cols["flow_b_1"][i])
        fa1 = float(cols["flow_a_1"][i])
        lvl1 = fb1 - fa1
        self.absflow_1_s.push(ts, abs(fb1) + abs(fa1))
        self.absflow_near_s.push(ts, near_abs)
        self.absflow_near_m.push(ts, near_abs)
        self.mlofi_1_s.push(ts, lvl1)
        self.mlofi_near_s.push(ts, near)
        self.mlofi_near_m.push(ts, near)
        self.mlofi_middle_s.push(ts, mid_band)
        self.mlofi_middle_m.push(ts, mid_band)
        self.mlofi_deep_s.push(ts, deep)
        self.mlofi_deep_m.push(ts, deep)

        # ---- ingest: lifecycle terminations / iceberg ------------------------
        tf = float(cols["term_filled"][i])
        tpt = float(cols["term_pulled_touched"][i])
        tpu = float(cols["term_pulled_untouched"][i])
        self.term_filled_l.push(ts, tf)
        self.term_pulled_touched_l.push(ts, tpt)
        self.term_pulled_untouched_l.push(ts, tpu)
        self.life_all_sum_l.push(
            ts, float(cols["life_filled_sum"][i] + cols["life_pulled_sum"][i])
        )
        self.life_unchained_sum_l.push(
            ts,
            float(
                cols["life_filled_unchained_sum"][i]
                + cols["life_pulled_unchained_sum"][i]
            ),
        )
        self.term_unchained_l.push(
            ts,
            float(cols["term_filled_unchained"][i] + cols["term_pulled_unchained"][i]),
        )
        self.refill_b_l.push(ts, float(cols["refill_b"][i]))
        self.refill_a_l.push(ts, float(cols["refill_a"][i]))
        self.refill_conf_l.push(ts, float(cols["refill_conf_sum"][i]))

        # ---- ingest: sweep detection & reclaim tracking ----------------------
        # a sweep = one-sided aggression trading through >= sweep_min_ticks
        # price levels within a single matching event
        tph, tpl = float(cols["t_px_high"][i]), float(cols["t_px_low"][i])
        swept_ticks = (tph - tpl) / tick if (t_buy + t_sell) > 0 else 0.0
        levels_swept = swept_ticks + 1.0 if (t_buy + t_sell) > 0 else 0.0
        one_sided = (t_buy > 0) != (t_sell > 0)
        if one_sided and levels_swept >= self.sweep_min_ticks and mid is not None:
            direction = 1.0 if t_buy > 0 else -1.0
            if direction > 0:
                self.sweep_buy_m.push(ts, swept_ticks)
            else:
                self.sweep_sell_m.push(ts, swept_ticks)
            self._resolve_sweep(ts)  # close any previous sweep first
            self._sweep = {
                "ts": ts,
                "dir": direction,
                "pre_mid": pre_mid if pre_mid is not None else mid,
                "extreme": (tph if direction > 0 else tpl) / tick,
                "max_retrace": 0.0,
            }
        else:
            self.sweep_buy_m.evict(ts)
            self.sweep_sell_m.evict(ts)

        sweep_failure = 0.0
        sweep_failure_signed = 0.0
        if self._sweep is not None:
            sw = self._sweep
            if ts - sw["ts"] > self.reclaim_ns:
                self._resolve_sweep(ts)
            elif mid is not None:
                move = abs(sw["extreme"] - sw["pre_mid"])
                if move > 0:
                    retrace = (sw["extreme"] - mid) if sw["dir"] > 0 else (mid - sw["extreme"])
                    sweep_failure = min(max(retrace / move, 0.0), 1.0)
                    sw["max_retrace"] = max(sw["max_retrace"], sweep_failure)
                    # reclaim points AGAINST the sweep: a failing buy sweep
                    # is bearish (negative), a failing sell sweep bullish
                    sweep_failure_signed = -sw["dir"] * sweep_failure
        self.failed_sweeps_l.evict(ts)

        # row state consumed by compose() (instantaneous values)
        self._row = (
            ts, mid, spread, valid, bid_px, ask_px, bid_sz, ask_sz,
            d_b_near, d_a_near, d_b_tot, d_a_tot, sweep_failure,
            float(cols["age_best_b"][i]), float(cols["age_best_a"][i]),
            sweep_failure_signed,
        )

    def compose(self) -> dict[str, float]:
        """Build the feature vector from current state (pure read)."""
        (ts, mid, spread, valid, bid_px, ask_px, bid_sz, ask_sz,
         d_b_near, d_a_near, d_b_tot, d_a_tot, sweep_failure,
         age_b_ns, age_a_ns, sweep_failure_signed) = self._row
        tick = self.tick

        out: dict[str, float] = {"ts": float(ts)}
        out["mid_ticks"] = mid if mid is not None else float("nan")
        out["spread_ticks"] = spread

        # Step 7 — Aggressive Delta (T-only, plan rule). Signed; ratio in
        # [-1, 1] with neutral 0 for an empty window.
        for tag, wb, ws_ in (
            ("s", self.t_buy_s, self.t_sell_s),
            ("m", self.t_buy_m, self.t_sell_m),
            ("l", self.t_buy_l, self.t_sell_l),
        ):
            delta = wb.sum - ws_.sum
            out[f"aggr_delta_{tag}"] = delta
            out[f"aggr_delta_ratio_{tag}"] = _ratio(delta, wb.sum + ws_.sum, 0.0)

        # Step 8 — Replenishment: share of best-level activity that RESTORES
        # liquidity: adds / (adds + fills + pulls) in [0,1]; neutral 0.5.
        out["replenish_bid_m"] = _ratio(
            self.add_best_b_m.sum,
            self.add_best_b_m.sum + self.fill_b_m.sum + self.pull_best_b_m.sum,
            0.5,
        )
        out["replenish_ask_m"] = _ratio(
            self.add_best_a_m.sum,
            self.add_best_a_m.sum + self.fill_a_m.sum + self.pull_best_a_m.sum,
            0.5,
        )

        # Step 9 — Price Progress: signed mid move (ticks, raw — sigma twin
        # is Phase 4A) + directional efficiency |net|/path in [0,1].
        for tag, wp in (("s", self.mid_past_s), ("m", self.mid_past_m)):
            past = wp.past(ts) if mid is not None else None
            out[f"price_progress_ticks_{tag}"] = (
                (mid - past) if (mid is not None and past is not None) else 0.0
            )
        out["directional_efficiency_m"] = _ratio(
            abs(out["price_progress_ticks_m"]), self.path_m.sum, 0.0
        )

        # Step 10 — Absorption: one-sided aggression that fails to move price
        # into a side that keeps replenishing. burst × stall × hold, all
        # dimensionless, in [0,1). burst = vol_s/(vol_s + expected_s), where
        # expected_s = long-window rate scaled to the short window (0.5 =
        # exactly average intensity). stall = 1/(1+|Δmid_s|). hold = short-
        # window replenishment of the absorbing side.
        exp_sell = self.t_sell_l.sum * (self.s / self.l)
        exp_buy = self.t_buy_l.sum * (self.s / self.l)
        burst_sell = _ratio(self.t_sell_s.sum, self.t_sell_s.sum + exp_sell, 0.0)
        burst_buy = _ratio(self.t_buy_s.sum, self.t_buy_s.sum + exp_buy, 0.0)
        # stall ingredient: |Δmid| in sigma units when the Phase 4A scale is
        # warm (composites from NORMALIZED ingredients, plan 4A rule 3);
        # raw ticks are the warm-up / standalone fallback
        move = abs(out["price_progress_ticks_s"])
        if self.sigma_provider is not None:
            _sig = self.sigma_provider()
            if _sig is not None and _sig > 0:
                move = move * self.tick_pts / _sig
        stall = 1.0 / (1.0 + move)
        hold_bid = _ratio(
            self.add_best_b_s.sum,
            self.add_best_b_s.sum + self.fill_b_s.sum + self.pull_best_b_s.sum,
            0.5,
        )
        hold_ask = _ratio(
            self.add_best_a_s.sum,
            self.add_best_a_s.sum + self.fill_a_s.sum + self.pull_best_a_s.sum,
            0.5,
        )
        out["absorption_bid_s"] = burst_sell * stall * hold_bid
        out["absorption_ask_s"] = burst_buy * stall * hold_ask
        out["absorption_net_s"] = out["absorption_bid_s"] - out["absorption_ask_s"]

        # Pulling/Stacking: net add-vs-pull flow near the touch, [-1,1];
        # neutral 0. Positive = stacking (liquidity building).
        st_b = _ratio(
            self.add_near_b_m.sum - self.pull_near_b_m.sum,
            self.add_near_b_m.sum + self.pull_near_b_m.sum,
            0.0,
        )
        st_a = _ratio(
            self.add_near_a_m.sum - self.pull_near_a_m.sum,
            self.add_near_a_m.sum + self.pull_near_a_m.sum,
            0.0,
        )
        out["stacking_bid_m"] = st_b
        out["stacking_ask_m"] = st_a
        out["stacking_net_m"] = st_b - st_a  # >0: bid side building vs ask

        # Price Impact: |mid move| per traded lot over the mid window
        # (ticks/lot, raw). 0 with no trades.
        vol_m = self.t_buy_m.sum + self.t_sell_m.sum
        out["price_impact_m"] = _ratio(abs(out["price_progress_ticks_m"]), vol_m, 0.0)

        # MLOFI (levels 1-10, near/middle/deep bands; plan Phase 3): signed
        # depth-flow imbalance, raw contract units.
        out["mlofi_1_s"] = self.mlofi_1_s.sum
        out["mlofi_near_s"] = self.mlofi_near_s.sum
        out["mlofi_middle_s"] = self.mlofi_middle_s.sum
        out["mlofi_deep_s"] = self.mlofi_deep_s.sum
        out["mlofi_near_m"] = self.mlofi_near_m.sum
        out["mlofi_middle_m"] = self.mlofi_middle_m.sum
        out["mlofi_deep_m"] = self.mlofi_deep_m.sum

        # Book Imbalance: (bid − ask)/(bid + ask) depth, [-1,1], neutral 0.
        out["book_imbalance_l1"] = _ratio(bid_sz - ask_sz, bid_sz + ask_sz, 0.0) if valid else 0.0
        out["book_imbalance_near"] = _ratio(d_b_near - d_a_near, d_b_near + d_a_near, 0.0)
        out["book_imbalance_total"] = _ratio(d_b_tot - d_a_tot, d_b_tot + d_a_tot, 0.0)

        # Microprice displacement from mid (ticks; positive = buy pressure).
        if valid and (bid_sz + ask_sz) > 0:
            micro = (bid_px * ask_sz + ask_px * bid_sz) / (bid_sz + ask_sz) / tick
            out["microprice_disp_ticks"] = micro - mid
        else:
            out["microprice_disp_ticks"] = 0.0

        # Liquidity Sweep activity (windowed swept ticks per direction).
        out["sweep_buy_ticks_m"] = self.sweep_buy_m.sum
        out["sweep_sell_ticks_m"] = self.sweep_sell_m.sum

        # Sweep Failure/Reclaim: fraction of the latest sweep's move already
        # retraced, [0,1]; failed sweeps counted over the long window.
        out["sweep_failure_score"] = sweep_failure
        out["failed_sweeps_l"] = float(len(self.failed_sweeps_l.buf))

        # Queue Depletion: executed volume at best vs what still stands,
        # [0,1); 0 = nothing depleted.
        out["queue_depletion_bid_s"] = _ratio(
            self.fill_b_s.sum, self.fill_b_s.sum + bid_sz, 0.0
        )
        out["queue_depletion_ask_s"] = _ratio(
            self.fill_a_s.sum, self.fill_a_s.sum + ask_sz, 0.0
        )

        # Order Survival (Phase 2 terminations): of orders the market
        # reached, the share that stood and executed rather than being
        # pulled, [0,1], neutral 0.5. Plus cancel-before-touch rate.
        out["liquidity_survival_ratio_l"] = _ratio(
            self.term_filled_l.sum,
            self.term_filled_l.sum + self.term_pulled_touched_l.sum,
            0.5,
        )
        out["cancel_before_touch_rate_l"] = _ratio(
            self.term_pulled_untouched_l.sum,
            self.term_pulled_touched_l.sum + self.term_pulled_untouched_l.sum,
            0.5,
        )

        # Iceberg score (heuristic, Critical Rule 8): probabilistic OR of
        # confidence-weighted refill frequency and hidden-volume share,
        # [0,1). Consumes the Phase 2 chain links.
        refills = self.refill_b_l.sum + self.refill_a_l.sum
        conf_mean = _ratio(self.refill_conf_l.sum, refills, 0.0)
        for side, refill_w, hidden_w, fill_w in (
            ("bid", self.refill_b_l, self.hidden_b_l, self.fill_b_l),
            ("ask", self.refill_a_l, self.hidden_a_l, self.fill_a_l),
        ):
            freq = _ratio(refill_w.sum, refill_w.sum + self.iceberg_k, 0.0)
            hidden_share = _ratio(hidden_w.sum, hidden_w.sum + fill_w.sum, 0.0)
            a = freq * conf_mean
            out[f"iceberg_score_{side}_l"] = 1.0 - (1.0 - a) * (1.0 - hidden_share)

        # Liquidity Vacuum: thinness ahead of price vs the long-window
        # baseline, [0,1); 0.5 = at baseline, ->1 = vacuum. "up" = thin ask
        # side above, "down" = thin bid side below.
        base_a = self.depth_a_mean_l.mean()
        base_b = self.depth_b_mean_l.mean()
        out["liquidity_vacuum_up"] = (
            1.0 - _ratio(d_a_near, d_a_near + base_a, 0.5) if base_a else 0.5
        )
        out["liquidity_vacuum_down"] = (
            1.0 - _ratio(d_b_near, d_b_near + base_b, 0.5) if base_b else 0.5
        )

        # Book Resiliency: recovery of near-touch depth from its mid-window
        # trough toward the long-window mean, [0,1]; 1 = fully recovered or
        # never depleted.
        for side, mn, mean_w, now in (
            ("bid", self.depth_b_min_m, self.depth_b_mean_l, d_b_near),
            ("ask", self.depth_a_min_m, self.depth_a_mean_l, d_a_near),
        ):
            lo = mn.min()
            mean = mean_w.mean()
            if lo is None or mean is None or mean <= lo:
                out[f"book_resiliency_{side}"] = 1.0
            else:
                out[f"book_resiliency_{side}"] = min(max((now - lo) / (mean - lo), 0.0), 1.0)

        # Trade Burst Intensity: short-window trade count vs the long-window
        # rate, [0,1); 0.5 = exactly average intensity.
        exp_n = self.t_n_l.sum * (self.s / self.l)
        out["trade_burst_intensity_s"] = _ratio(self.t_n_s.sum, self.t_n_s.sum + exp_n, 0.0)

        # Queue Turnover: best-level churn (adds+pulls) vs standing depth,
        # [0,1); 0 = static queue.
        churn_b = self.add_best_b_m.sum + self.pull_best_b_m.sum
        churn_a = self.add_best_a_m.sum + self.pull_best_a_m.sum
        out["queue_turnover_bid_m"] = _ratio(churn_b, churn_b + bid_sz, 0.0)
        out["queue_turnover_ask_m"] = _ratio(churn_a, churn_a + ask_sz, 0.0)

        # Order Lifetime: mean lifetime of terminated orders (ms, raw) and
        # the chain-adjusted twin (iceberg refill clips excluded — Phase 2
        # showed refills are ~100x shorter-lived and would poison this).
        n_term = self.term_filled_l.sum + self.term_pulled_touched_l.sum + self.term_pulled_untouched_l.sum
        out["order_lifetime_ms_l"] = _ratio(self.life_all_sum_l.sum, n_term, 0.0) / 1e6
        out["order_lifetime_chain_adj_ms_l"] = (
            _ratio(self.life_unchained_sum_l.sum, self.term_unchained_l.sum, 0.0) / 1e6
        )

        # Liquidity Age: size-weighted age of the best level (seconds, raw)
        # + bounded age imbalance ([-1,1]; positive = older bid liquidity).
        age_b = age_b_ns / NS
        age_a = age_a_ns / NS
        out["liquidity_age_bid_s"] = age_b
        out["liquidity_age_ask_s"] = age_a
        out["liquidity_age_imbalance"] = _ratio(age_b - age_a, age_b + age_a, 0.0)

        # ---- Step 20 gate iteration: signed-asymmetry set --------------------
        # The Step 20 diagnosis: move-timing AUC 0.880, sign AUC 0.556 — the
        # set below exposes the SIGN content of ingredients the v1 features
        # mostly emitted as magnitudes or per-side pairs. All positive =
        # buy/bid-supportive; bounded [-1,1] (dimensionless, do-not-normalize)
        # except sweep_net_ticks_m (tick distance -> sigma twin in Phase 4A).

        # signed depth-flow imbalance as a bounded ratio: net flow over gross
        # flow magnitude — scale-free, so trees see sign, not volume regime
        out["flow_imbalance_1_s"] = _ratio(self.mlofi_1_s.sum, self.absflow_1_s.sum, 0.0)
        out["flow_imbalance_near_s"] = _ratio(
            self.mlofi_near_s.sum, self.absflow_near_s.sum, 0.0
        )
        out["flow_imbalance_near_m"] = _ratio(
            self.mlofi_near_m.sum, self.absflow_near_m.sum, 0.0
        )

        # execution-side imbalance: resting-ask fills = aggressive buying
        out["fill_imbalance_s"] = _ratio(
            self.fill_a_s.sum - self.fill_b_s.sum,
            self.fill_a_s.sum + self.fill_b_s.sum, 0.0,
        )
        out["fill_imbalance_m"] = _ratio(
            self.fill_a_m.sum - self.fill_b_m.sum,
            self.fill_a_m.sum + self.fill_b_m.sum, 0.0,
        )
        # hidden executions on the BID side = hidden buyer absorbing sells
        out["hidden_fill_imbalance_l"] = _ratio(
            self.hidden_b_l.sum - self.hidden_a_l.sum,
            self.hidden_b_l.sum + self.hidden_a_l.sum, 0.0,
        )
        # iceberg presence asymmetry (bid-side hidden replenishment = support)
        out["iceberg_asym_l"] = out["iceberg_score_bid_l"] - out["iceberg_score_ask_l"]

        # ask-side liquidity fleeing (pulls at/near the touch) = upside vacuum
        pull_a = self.pull_best_a_m.sum + self.pull_near_a_m.sum
        pull_b = self.pull_best_b_m.sum + self.pull_near_b_m.sum
        out["pull_imbalance_m"] = _ratio(pull_a - pull_b, pull_a + pull_b, 0.0)

        # side tilts of existing per-side scores (each term already [0,1])
        out["vacuum_tilt"] = out["liquidity_vacuum_up"] - out["liquidity_vacuum_down"]
        out["resiliency_tilt"] = out["book_resiliency_bid"] - out["book_resiliency_ask"]
        out["depletion_tilt_s"] = (
            out["queue_depletion_ask_s"] - out["queue_depletion_bid_s"]
        )
        out["replenish_tilt_m"] = out["replenish_bid_m"] - out["replenish_ask_m"]
        out["turnover_tilt_m"] = out["queue_turnover_bid_m"] - out["queue_turnover_ask_m"]

        # book-shape sign: imbalance beyond the near band, its tilt vs the
        # near band, and near-band depth concentration per side
        d_b_outer = d_b_tot - d_b_near
        d_a_outer = d_a_tot - d_a_near
        out["book_imbalance_outer"] = _ratio(
            d_b_outer - d_a_outer, d_b_outer + d_a_outer, 0.0
        )
        out["imbalance_tilt"] = out["book_imbalance_near"] - out["book_imbalance_outer"]
        out["depth_concentration_tilt"] = _ratio(d_b_near, d_b_tot, 0.5) - _ratio(
            d_a_near, d_a_tot, 0.5
        )

        # signed sweep set: net swept ticks, the running reclaim AGAINST the
        # active sweep, and the mean direction of recent failed sweeps
        # (failed SELL sweeps = bullish reclaim, hence the negation)
        out["sweep_net_ticks_m"] = self.sweep_buy_m.sum - self.sweep_sell_m.sum
        out["sweep_failure_signed"] = sweep_failure_signed
        out["failed_sweep_net_ratio_l"] = -_ratio(
            self.failed_sweeps_l.sum, float(len(self.failed_sweeps_l.buf)), 0.0
        )

        return out

    # -- helpers ---------------------------------------------------------------

    def _resolve_sweep(self, ts: int) -> None:
        """Close the active sweep; count it as failed if enough retraced.
        The pushed value is the sweep's SIGNED direction (±1): the buffer
        length is still the failed-sweep count (failed_sweeps_l feature),
        while the sum carries direction (failed_sweep_net_ratio_l)."""
        sw = self._sweep
        if sw is not None and sw["max_retrace"] >= self.failed_retrace:
            self.failed_sweeps_l.push(ts, sw["dir"])
        self._sweep = None

    def run(self, cols: dict, sample_every_ns: int = 0):
        """Historical driver: stream every row through step() (the one code
        path), keeping one output row per `sample_every_ns` (0 = keep all).
        Returns (names, list of feature dicts kept)."""
        n = len(cols["ts"])
        kept: list[dict[str, float]] = []
        next_keep = 0
        for i in range(n):
            self.ingest(cols, i)
            ts = int(cols["ts"][i])
            if sample_every_ns == 0 or ts >= next_keep or i == n - 1:
                kept.append(self.compose())  # pure read of ingested state
                if sample_every_ns:
                    next_keep = ts + sample_every_ns
        return self.names, kept
