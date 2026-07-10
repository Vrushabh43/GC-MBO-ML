# Phase 4/4A Completion Report — Normalization Architecture

Date: 2026-07-10 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Phase 4 + Phase 4A (canonical)

## Verdict: **COMPLETE**

The three canonical scales, the `_norm` twins for everything the plan's
"what gets normalized where" list assigns to features, rolling percentiles
and robust z-scores, session-boundary resets, and all four Phase 4A
verification requirements implementable today (era invariance, one code
path / train-serve parity, determinism, serialized scale definitions).
Label-side normalization (sigma_h labels, time-to-extreme fractions)
belongs to Phase 7 and consumes the same `ScaleEngine`.

---

## 1. The three canonical scales (`src/features/normalization.py`)

All strictly past-only, robust (rolling median), updated on a 1-second
clock, identical streaming objects for historical and live:

- **sigma_h(t)** — median absolute h-horizon mid move (points), h ∈
  {30 s, 2 m, 10 m} (config `sigma_horizons_s`), trailing 60 min.
- **v_scale(t)** — median contracts-per-second, trailing 60 min
  (zero-volume seconds count: quiet is real market state).
- **d_scale(t)** — median combined near-book depth, trailing 60 min.

**Resets**: at the 18:00-ET-anchored trading-day boundary — this matters
inside single files: every daily file contains the 17:00–18:00 ET
maintenance break, and the real Sunday file demonstrably warms during
pre-open, resets at the open, and re-warms (tested). Roll resets follow
from per-session processing + the Step 12.5 `crosses_roll` guard.

**Warm-up**: each scale is None until `min_samples`; its twins are NaN
exactly that long (per-scale), and `norm_ready` (all three warm) feeds the
Phase 11 warm-up safety gate. Scales are also emitted as regime
information: raw + past-only session percentile.

## 2. What got normalized (plan 4A rules, explicit twin maps)

| Rule | Features | Twin |
|---|---|---|
| 1 — tick distances | price_progress_s/m, microprice_disp, sweep_buy/sell_ticks | × tick_pts / sigma(2 m) |
| 2a — flow volumes | aggr_delta_s/m/l | ÷ (v_scale × window seconds) |
| 2b — depth flow | mlofi_1/near/middle/deep × s/m | ÷ d_scale |
| 3 — composites | **absorption stall ingredient upgraded**: \|Δmid\| now in sigma units once warm (raw-tick fallback during warm-up/standalone) — composites are built from normalized INGREDIENTS, never re-scaled | — |
| 3 — dimensionful ratio | price_impact_m_norm rebuilt from normalized ingredients ((pts/σ) per (vol/v·W)) | — |
| do-not-normalize | every bounded score, spread (its percentile IS emitted), ages/lifetimes (log-transform is the Phase 5 standardization layer), categoricals | passthrough, byte-identical (tested) |

Phase 4 additions: rolling **percentiles** (config
`[normalization.percentiles]`; plan's `absorption_net` @60 m example +
spread + burst) and **robust z-scores** ((x−median)/MAD, streaming
approximation documented; config `[normalization.robust_z]`, default
minimal because 4A twins already cover volumes/distances).

**Documented decisions**: feature distances use sigma(2 m) (config
`distance_sigma_horizon_s`; labels will use their own h per Phase 7);
d_scale uses the near+mid bands (levels 1–6) because the flow stream
aggregates bands and the plan's top-5 default is not band-decomposable;
baselines are sample-weighted (as in Phase 3).

## 3. Files created or changed

| File | Change |
|---|---|
| `src/features/normalization.py` | **new** — RollingMedian/Percentile/RobustZ, ScaleEngine, NormalizedFeatureEngine (84 outputs/step), scale fingerprint for model bundles |
| `src/features/core_features.py` | `sigma_provider` hook: absorption stall ingredient in sigma units when warm |
| `config/config.toml` | `[normalization]` (+ percentiles / robust_z sub-tables) |
| `tests/test_normalization_synthetic.py` | **new** — 16 tests |
| `tests/test_normalization_real.py` | **new** — 9 tests |

## 4. Tests run and results

| Suite | Result |
|---|---|
| Synthetic (16): median/percentile/robust-z primitives (known answers), exact v/d scale values on a constructed session, sigma on a known move pattern, warm-up NaN → ready, **maintenance-boundary reset**, exact twin arithmetic, **ERA INVARIANCE — same relative pattern at 1× vs 3× price AND volume through the real Rust engine: scales scale ×3, every `_norm` twin and bounded composite matches to 1e-6** (plan 4A verification), raw-feature passthrough, one-code-path step≡run (train/serve parity of scales), **past-only/no-leakage** (outputs unchanged by future rows), determinism, percentile bounds, robust-z feature emission, bundle fingerprint | **16/16 PASS** |
| Real data, 2026-01-04 (9): pre-open warm → reset at 18:00 ET open → re-warm ~150 s (the reset observed on real data); scale plausibility (v < 1000 c/s, σ₂ₘ < 50 pts, moves > 0); sigma ordering σ₃₀ₛ ≤ σ₂ₘ ≤ σ₁₀ₘ; twin arithmetic exact on 1000+ sampled rows; percentile bounds; passthrough with absorption-only differences (upgrade engages on real data); absorption still bounded; per-scale NaN semantics; determinism | **9/9 PASS** |
| Full suite (Phases 1–4 + Step 12.5) | **165/165 PASS** |

## 5. Known limitations

1. **Throughput**: full normalized composition runs at ~8k group-rows/s
   (~5.5 min per heavy session; ~25 h full archive on 10 workers). The
   clean optimization — ingest every group but compose twins/percentiles
   only at sample instants (Phase 6 samples ~1 Hz) — is an in-place change
   to `run()`, not a second code path. Do it when Phase 5/6 fixes the
   sampling cadence.
2. **Streaming MAD approximation** for robust z (deviations vs push-time
   median) — standard, documented; exact windowed MAD would rescan.
3. **d_scale bands** (1–6) vs the plan's top-5 default — band
   decomposition limit, documented above.
4. sigma can be legitimately 0 in dead stretches (median of zero moves);
   division floors at `SCALE_EPS` and the stall ingredient guards zero —
   winsorization at ±8 (Phase 5 input layer) bounds any residual blow-ups.

## 6. Phase gate

- Three canonical scales, past-only, robust, reset semantics: **DONE, tested on real data**
- `_norm` twins per the 4A assignment lists; do-not-normalize respected: **DONE (byte-identical passthrough tested)**
- Composites from normalized ingredients (absorption upgraded): **DONE**
- Rolling percentiles + robust z-scores (Phase 4 proper): **DONE**
- Era invariance (1× vs 3× price+volume): **PASS**
- One code path (live step ≡ historical run; scales identical): **PASS**
- Scale definitions serializable for model bundles: **DONE** (`scale_config_fingerprint`)

**Next per the build order: Phase 5** (Steps 13–17) — exact-event inputs,
adaptive flow bars, 1 s tactical / 10 s slow context, and the regime vector
(consuming these scales, the Step 12.5 calendar features, and
days-to-expiry from the roll ledger).
