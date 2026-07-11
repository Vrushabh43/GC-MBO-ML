# Phase 6 Completion Report — Labels and Training-Sample Index

Date: 2026-07-11 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Build Order Steps 18–19 (plan Phase 6 + Phase 7 label definitions)

## Verdict: **COMPLETE** — with label hygiene explicitly limited by the FOMC-only macro calendar (standing open item)

---

## 1. Step 18 — labels on tradeable prices (`src/labeling/labels.py`)

- **Convention (stored as metadata with every file)**: `touch_opposite` —
  long enters at the best ask (conservatively: the sample second's WORST
  ask), favorable path marked at the best-bid path; short mirrored. Cost
  model explicit and raw: 2.0 ticks round trip (config; never normalized —
  Critical Rule 21).
- **Per horizon (30 s primary / 2 m secondary / 10 m experimental)**:
  upside, downside, adverse (both sides), final return, time-to-high/low —
  in **dual units** (points AND ÷sigma_h(t), v2.1) with time fractions of
  horizon (4A rule 4); sigma_h(t) stored per sample.
- **Direction classes**: BULLISH iff cost-adjusted favorable ≥ 2× the
  cost-adjusted adverse (config ratio) — the ratio test is scale-free, so
  eras compare identically; **adverse floored at 0 in the decision** (a
  gap that never retraces earns no negative-risk credit — found by test:
  the unfloored rule labeled 1-tick net moves BULLISH). NO_TRADE therefore
  means "not worth trading after costs".
- **Label path**: per-second bid/ask extremes with carry-forward of each
  second's CLOSING quote through quiet seconds (the resting book
  persists); second-resolution times-to-extreme (documented).
- Labels are supervision with no live counterpart → vectorized numpy is
  permitted; the one-code-path rule binds trigger/feature computation,
  which stays streaming.

## 2. Step 19 — sample index (`src/datasets/sample_index.py`)

- **Selection (past-only, live-reusable pure function)**: ~1 Hz clock
  samples while `norm_ready`, plus event triggers — sweep, absorption
  onset (crossing), MLOFI shift, trade burst, queue collapse, vacuum,
  major replenishment — under the minimum-spacing rule (0.5 s) and
  per-type-per-session caps (300; the queue-collapse cap actually binds
  on real sessions).
- **Hygiene per sample**: release policy over the label window via the
  Step 12.5 calendar (exclude/tag/ok) + loud coverage flag; windows never
  span the maintenance break (session-id check); `label_end_ts` stored as
  the Phase 9 purging anchor; cold (unwarmed) periods produce no samples.
- **Overlap as a first-class problem**: per-horizon López-de-Prado
  average-uniqueness weights; per-session and total **effective-N
  reporting** (`reports/sample_index.md`).

## 3. Dev-slice results (12 sessions, `reports/sample_index.md`)

| horizon | trainable rows | effective N | effective share |
|---|---|---|---|
| 30 s | 700,453 | **27,176** | 3.9% |
| 2 m | 708,963 | 6,982 | 1.0% |
| 10 m | 706,039 | 1,475 | 0.2% |

- Class balance is stable across all 12 sessions (NO_TRADE 0.82–0.91,
  BULLISH ≈ BEARISH ≈ 0.04–0.07) — the cost-aware, vol-normalized rule
  behaves era-consistently; costs dominating at 30 s is the expected
  economics, not a bug.
- The plan's central Phase 6 warning is now quantified: **row count
  overstates information 25× at 30 s and 480× at 10 m.** Every training
  set must be sized by effective N (R5 discipline).

## 4. Files created or changed

| File | Change |
|---|---|
| `src/labeling/labels.py` | **new** — SecondPath builder + LabelEngine (dual-unit labels, direction classes, metadata) |
| `src/datasets/sample_index.py` | **new** — TriggerState (live-reusable), uniqueness weights, session builder + parquet writer |
| `scripts/build_sample_index.py` | **new** — dev-slice driver + effective-N report |
| `config/config.toml` | `[labels]` + `[samples]` sections |
| `tests/test_labels_synthetic.py` | **new** — 17 tests |
| `tests/test_samples_real.py` | **new** — 10 tests |
| `data/sample_index/samples-*.parquet` | 12 session indexes (~700k samples) |

## 5. Tests run and results

| Suite | Result |
|---|---|
| Synthetic (17): path construction incl. closing-quote carry-forward; upside/adverse/times exact on constructed paths; direction BULLISH/BEARISH/NO_TRADE incl. cost-eats-the-move; incomplete-window flagging; mid-based final return; dual-unit normalization; metadata; uniqueness weights (isolated=1, overlapping known value, dense 1 Hz ≈ 1/h); clock cadence + spacing; warm-up produces no samples; release exclusion inside the window; trainability requirements; effective-N < rows; determinism | **17/17 PASS** |
| Real, 2026-01-04 (10): counts + caps binding; global min-spacing; sigma present for complete windows; label geometry (window high ≥ low both sides); **monotone envelope** (10 m upside ≥ 30 s upside per sample); class balance non-degenerate; direction consistent with stored labels + cost; hygiene columns; weights ∈ (0,1], effective share decreasing with horizon; determinism | **10/10 PASS** |
| Full suite (Phases 1–6 + Step 12.5) | **218/218 PASS** |

## 6. Known limitations

1. **Label hygiene is FOMC-only** until the macro release calendar
   (CPI/PPI/NFP/PCE/GDP/auctions) is ingested — `release_policy` is
   currently blind to those (the coverage flag exists but FOMC events
   keep the 45-day window "covered"). **Must be closed before Phase 8
   training on real stakes** — repeated standing item.
2. **Pipeline speed**: the dev-slice index build took ~7.5 h serial
   (~40 min per weekday session). Before ANY full-archive run: (a) apply
   the Phase 4 compose-at-sample-instants optimization (~10×), (b)
   parallelize the driver per session (10 workers) and make it skip
   existing outputs, like the volume scan. In-place changes, not new
   paths.
3. **Second-resolution label paths** (times-to-extreme quantized to 1 s;
   entry at the second's worst quote is conservative by construction).
4. Uniqueness weights are per-session (windows never span sessions, so
   cross-session concurrency is impossible by design).
5. Event-trigger thresholds are v1 defaults in config; Phase 8 feature
   importance may motivate recalibration (they only affect the EXTRA
   samples on top of the 1 Hz clock).

## 7. Phase gate

- Labels: tradeable-price convention + explicit cost + dual units +
  direction classes in vol units + metadata: **DONE, tested**
- Sampling: 1 Hz + event triggers + spacing + caps (past-only, live-
  reusable): **DONE, tested**
- Hygiene: release policy, session containment, purge anchor: **DONE**
- Uniqueness weights + effective-N report: **DONE** (dev slice: 27k
  effective at 30 s from 700k rows)
- Determinism: **PASS**

**Next per the build order: Step 20 — Model A baseline (30 s horizon) and
the GO/NO-GO GATE**, with pre-registered criteria recorded in config
BEFORE training (plan Phase 8). Prerequisites to close first:
(a) macro release calendar ingestion (label hygiene),
(b) the pipeline speed items above (the gate needs ≥20 out-of-sample
trading days = a bigger slice than the dev window),
(c) purged/embargoed split machinery (Phase 9 rules, consumed by the gate
evaluation).
