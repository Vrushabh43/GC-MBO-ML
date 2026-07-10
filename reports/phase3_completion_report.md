# Phase 3 Completion Report — Core Order-Flow Features

Date: 2026-07-10 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Build Order Steps 7–12

## Verdict: **COMPLETE**

All nineteen plan Phase 3 features are built in the plan's build order
(Aggressive Delta → Replenishment → Price Progress → Absorption →
remaining features one by one), each shipping the three plan-mandated
artifacts: a known-answer unit test on synthetic event sequences, a
property test (bounds, sign conventions), and a one-session visual sanity
notebook section. Absorption additionally passes the Step 11 numeric gate.

---

## 1. Architecture (the plan's Phase 0 boundary, enforced)

- **Rust core emits FACTS** (`core/src/flow.rs`): one row of flow
  primitives per matching-event group for one tracked contract — T-only
  aggressor volumes and trade-price extremes, add/pull volume at and near
  the touch, fills and hidden (beyond-displayed) volume per side,
  per-level depth-flow for levels 1–10 (MLOFI ingredients), near/middle/
  deep depth aggregates, post-group best quotes, lifecycle termination
  tallies (filled / pulled-touched / pulled-untouched, raw and
  chain-adjusted lifetimes), iceberg refill links with confidence, and
  size-weighted best-level liquidity age. 69 columns, drained to numpy
  exactly like the Phase 2 lifecycle records.
- **Python composes FEATURES** (`src/features/`): a streaming
  `FeatureEngine` whose `step(cols, i)` is the ONLY computation path —
  historical replay feeds it drained columns row by row; live feeds the
  same columns of length 1. **No vectorized-historical variant exists**
  (Critical Rule 3; the plan's rejected-option list). Verified by a test
  that runs the same buffer through both drivers and asserts equality.
- **Raw values only**: tick distances, contract volumes, and ms lifetimes
  stay raw; `_norm` twins are Phase 4A's job. Composite scores are built
  from dimensionless ratios of like quantities, so they are bounded and
  era-scale-free by construction — verified by a 3× volume-scaling
  invariance test (plan Phase 3 addition), and consistent with the
  Phase 4A do-not-normalize list (bounded scores are never re-scaled).

## 2. The feature set (window suffixes: s/m/l = 2 s/10 s/60 s, config)

| Plan feature | Outputs (raw) | Definition sketch |
|---|---|---|
| Aggressive Delta | aggr_delta_{s,m,l}, ratio twins [-1,1] | T-only buy−sell volume (Phase 1 rule; F never counts) |
| Replenishment | replenish_{bid,ask}_m [0,1] | adds/(adds+fills+pulls) at best; 0.5 neutral |
| Price Progress | price_progress_ticks_{s,m}, directional_efficiency_m [0,1] | signed mid move; \|net\|/path |
| Absorption | absorption_{bid,ask}_s [0,1), absorption_net_s | burst×stall×hold: volume intensity vs 60s rate × 1/(1+\|Δmid\|) × best-level hold; scale-invariant |
| Pulling/Stacking | stacking_{bid,ask}_m [-1,1], net | (adds−pulls)/(adds+pulls) near touch |
| Price Impact | price_impact_m | \|Δmid ticks\| per traded lot |
| MLOFI | mlofi_1_s, mlofi_{near,middle,deep}_{s,m} | per-level depth-flow imbalance, levels 1–3/4–6/7–10 |
| Book Imbalance | book_imbalance_{l1,near,total} [-1,1] | (bid−ask)/(bid+ask) depth |
| Microprice | microprice_disp_ticks | size-weighted micro − mid; + = buy pressure |
| Liquidity Sweep | sweep_{buy,sell}_ticks_m | one-sided groups trading through ≥2 levels |
| Sweep Failure/Reclaim | sweep_failure_score [0,1], failed_sweeps_l | retraced fraction of last sweep within 10 s; ≥0.8 ⇒ failed |
| Queue Depletion | queue_depletion_{bid,ask}_s [0,1) | fills at best/(fills+standing depth) |
| Order Survival | liquidity_survival_ratio_l, cancel_before_touch_rate_l [0,1] | Phase 2 terminations: filled vs pulled-after-touch |
| Iceberg Probability | iceberg_score_{bid,ask}_l [0,1) | prob-OR of confidence-weighted refill rate and hidden-volume share (heuristic, Critical Rule 8) |
| Liquidity Vacuum | liquidity_vacuum_{up,down} [0,1) | 1 − depth/(depth+60s baseline); 0.5 = baseline |
| Book Resiliency | book_resiliency_{bid,ask} [0,1] | recovery from 10s depth trough toward 60s mean |
| Trade Burst Intensity | trade_burst_intensity_s [0,1) | trade count vs 60s rate; 0.5 = average |
| Queue Turnover | queue_turnover_{bid,ask}_m [0,1) | best-level churn/(churn+depth) |
| Order Lifetime | order_lifetime_ms_l + chain-adjusted twin | mean terminated lifetime; refill clips excluded in twin |
| Liquidity Age | liquidity_age_{bid,ask}_s, age imbalance [-1,1] | size-weighted best-level age |

Neutral naming throughout (pulling/stacking, cancel-before-touch,
refills); nothing is labeled as intent.

## 3. Files created or changed

| File | Change |
|---|---|
| `core/src/flow.rs` | **new** — per-group flow-primitive recorder |
| `core/src/engine.rs` | recorder hooks in T/A/C/F/R arms + group-flush emission |
| `core/src/book.rs` | `record_fill` also returns hidden (beyond-displayed) quantity |
| `core/src/lifecycle.rs` | `on_add`/`on_terminate` return link/termination info for the recorder |
| `core/src/lib.rs` | `enable_flow`, `flow_drain` (69 columns), `flow_stats` |
| `src/features/windows.py` | **new** — streaming window primitives (sum/count/past/min/mean), all past-only |
| `src/features/core_features.py` | **new** — the 19-feature streaming FeatureEngine (~55 outputs) |
| `src/features/flow_stream.py` | **new** — session driver (front-contract lookup + flow replay) |
| `src/mbo_engine/engine.py`, `src/utilities/config.py`, `config/config.toml` | flow/feature config plumbing (`[features]`) |
| `tests/test_features_synthetic.py` | **new** — 30 known-answer tests through the real Rust path |
| `tests/test_features_real.py` | **new** — 9 property/reconciliation tests on a real session |
| `scripts/build_phase3_visual_notebook.py` | **new** — builds + executes the visual notebook |
| `notebooks/phase3_core_features_visual.ipynb` | **new** — 15 executed figure sections, one per feature group |
| `benchmarks/throughput_bench.py` | third benchmarked config: book+lifecycle+flow |

## 4. Tests run and results

| Suite | Result |
|---|---|
| `tests/test_features_synthetic.py` — 30 known-answer tests: T-only delta (F excluded), window expiry, replenishment incl. neutral drain, signed progress + efficiency round trip, absorption value/one-sidedness/price-moving-kills-it, **3× volume scale invariance of composites**, stacking, price impact, MLOFI level flows, imbalance, microprice sign, sweep detect/measure/reclaim-failure/hold, queue depletion + turnover, survival + cancel-before-touch, iceberg refill + hidden volume, lifetime chain adjustment, trade burst (lone spike + steady-state 0.5), liquidity age weighting, vacuum, resiliency trough/recovery, **one-code-path equality (live step ≡ historical run)**, determinism | **30/30 PASS** |
| `tests/test_features_real.py` — 9 tests on 2026-01-04: flow trade volume == engine T volume (independent counter); termination/refill tallies == Phase 2 lifecycle records (two paths, exact); book-state sanity; replay-twice byte-identical primitives; all features finite; all bounds hold; signed features use both signs; **every feature moves on real data** (dead-pipe detector); feature determinism | **9/9 PASS** |
| Full suite (Phases 1+2+3) | **120/120 PASS** |
| `benchmarks/throughput_bench.py` (2026-01-06, 4.38M records) | **PASS** — book ~4.6M ev/s; +lifecycle ~2.9M; **+flow ~1.19M ev/s (6× sustained target, 2.4× burst target)**; identical state digests across all three configs, replay-twice deterministic |
| Step 11 absorption gate (notebook, real session) | **PASS** — at top-decile absorption, median \|Δmid 2s\| = 0.5 ticks vs 1.0 overall, on above-average volume |

## 5. Known limitations / decisions

1. **Feature composition speed**: the Python streaming step runs ~28k
   group-rows/s (~90 s per heavy session; the full archive is a ~8 h batch
   on 10 workers). Acceptable for research; if it binds, optimize the
   step() internals — never by adding a vectorized second path.
2. **Modify-driven size changes** are not in the add/pull primitives
   (A/C actions only); they ARE fully captured in the per-level depth-flow
   (MLOFI) columns. Extension candidate if a feature needs modify flow
   separated.
3. **WindowMean/baselines are sample-weighted** (per group row), not
   time-weighted — busy periods weigh more. Documented choice; revisit in
   Phase 4A if baselines matter for scales.
4. **Front-contract lookup is a cheap pre-replay** per session until the
   Step 12.5 roll ledger exists.
5. **Sweep detection is per matching event** (single-group sweeps);
   multi-group sweep stitching is a Phase 5 flow-bar concern.
6. `[features] flow_dir` is declared for optional primitive dumps; nothing
   writes it yet (features are recomputed from raw replay, which is the
   deterministic source of truth).

## 6. Phase gate

- Steps 7–10 built in order, Step 11 visual+numeric absorption test: **DONE**
- Step 12 all remaining features, each with unit + property + visual: **DONE**
- T-only volume rule respected everywhere (unit-tested): **VERIFIED**
- Composite scores scale-invariant (3× volume test): **VERIFIED**
- One code path (live step ≡ historical, no vectorized variant): **VERIFIED**
- Determinism (primitives + features, replay-twice): **PASS**
- Throughput with full recording: **PASS** (1.19M ev/s ≥ 200k/500k)

**Next per the build order: Step 12.5** — economic-calendar module + roll
policy + full-archive active-contract ledger — then Phase 4/4A
(normalization architecture: sigma_h, v_scale, d_scale and the `_norm`
twins for everything above).
