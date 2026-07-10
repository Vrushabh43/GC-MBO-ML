# Phase 5 Completion Report — Final Model Input Architecture

Date: 2026-07-10 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Build Order Steps 13–17

## Verdict: **COMPLETE** (standardization statistics deliberately unfitted — they are Phase 8 training-set artifacts)

All five plan inputs are assembled by ONE streaming consumer
(`src/datasets/inputs.py::InputAssembler.step`), past-only, deterministic,
identical historical/live, consuming everything the earlier phases built:
flow primitives (P3), normalized features + scales (P4/4A), calendar +
session phases + roll ledger (Step 12.5).

---

## 1. The five inputs (dims as built; 1,024/64/256 remain config)

| # | Input | Shape | Content |
|---|---|---|---|
| 1 | Exact event stream | last **1,024 events × 22** | per matching event: inter-event dt, buy/sell/near-add/near-pull/fill/hidden volumes (v-normalized at event time), trade distance from mid + mid change (sigma units), spread, L1 imbalance, microprice disp (norm), MLOFI-1, best-level liquidity ages, lifecycle terminations, refill links |
| 2 | Adaptive flow bars | last **256 bars × 30** | close on **64 events or 15 s**, whichever first: duration, event/trade counts, buy/sell/delta + side add/pull/fill/hidden volumes (normalized by v·duration), bar replenishment ratios, close-open + range (sigma units), MLOFI sums (÷d), swept ticks, terminations/refills, events/s, close-state scores |
| 3 | Tactical context | last **300 s × 23** at 1 s | per-second delta/volume/event increments (exact zeros in quiet seconds) + the plan's input-3 feature list (absorption, MLOFI, impact, sweeps + failure, depletion, survival, replenishment, resiliency, vacuum, burst) + 1 s mid change + spread |
| 4 | Slow context | last **180 × 15** at 10 s | 10 s return + range (sigma units), delta/volume/event increments, CVD slope, mean absorption, failed sweeps, MLOFI trend, depth (÷d), spread, burst, resiliency, directional efficiency |
| 5 | Regime vector | **71 dims** | 11 activity bases × 5 percentile windows (1m/5m/15m/60m/session-cap, past-only) + sigma/v/d raw+percentile + norm_ready + calendar (seconds to/since scheduled event capped at 7 d, tiers, blackout flag, session phase, settlement flag) + **days-to-expiry / days-since-roll from the Step 12.5 ledger** |

Per plan: tensors carry the **era-normalized variants**, normalized at
event time with then-current past-only scales; raw values remain in the
feature store. Warm-up ⇒ NaN entries + `norm_ready=False` on the sample —
Phase 6 must not select cold samples (Phase 11 warm-up gate).

**GRANULARITY DECISION (Input 1, documented conflict-of-letter):** "event"
= one CME matching event (ts_event group) — the atomic market action the
Phase 1 engine reconstructs — not one raw MBO record, which splits a
single fill into mechanical T/F/C(/M) rows the engine already folds
semantically. Exact event order is preserved; 1,024 matching events span
strictly more market history than 1,024 raw records; order-age/queue
context enters via best-level liquidity age + per-event lifecycle fields.
Flag raised to the user in-session; revisit if raw-record granularity is
ever wanted (the flow recorder would emit per-record rows instead).

## 2. Input-standardization layer (`src/datasets/standardize.py`)

signed-log1p on configured heavy-tailed dims → robust (median/scale)
standardization → winsorize ±8 with clip counts returned (the Phase 10
drift signal). **Scale fallback chain** (found on real tensors): MAD → mean
absolute deviation → 1.0, because zero-inflated dims (sweep scores,
depletion: 0 most seconds) have MAD exactly 0 and would clip every nonzero
value (observed 49% clipping → 6% after the fix, <5% across a session).
Statistics are **fit on the training set only (Phase 8)**; fit/transform/
serialize round-trip is byte-identical (train/serve parity tested). NaNs
pass through (masking is the sampler/model's job) — the layer never
creates one.

## 3. Files created or changed

| File | Change |
|---|---|
| `src/datasets/inputs.py` | **new** — InputAssembler (5 inputs, one step), InputSample, assemble_session driver (ledger/calendar wired) |
| `src/datasets/standardize.py` | **new** — InputStandardizer (fit/transform/save/load) |
| `config/config.toml` | `[inputs]` section (windows, bar rules, regime windows, winsorize bound) |
| `tests/test_inputs_synthetic.py` | **new** — 17 tests |
| `tests/test_inputs_real.py` | **new** — 9 tests |

## 4. Tests run and results

| Suite | Result |
|---|---|
| Synthetic (17): event ring order/window/known content/left-padding; bar close on event count AND on max duration; bar volume accounting (delta = −sells in a sell-only session); tactical cadence + known per-second increments; slow cadence + flat-session zero return; regime percentile bounds; calendar integration (seconds-to-event exact, tier, blackout inside pre-window); **past-only** (sample at t identical with future rows removed); determinism; standardizer known answers, winsorize counts, **serialize round-trip parity**, unfitted refusal | **17/17 PASS** |
| Real, 2026-01-04 (9): full ring occupancy post-warm-up; no NaN in ready tensors; event-stream plausibility; bars ≤ 64 events, median duration ≤ 15 s (quiet-gap overshoot documented); regime bounds + **days-to-expiry exactly matches the roll ledger** + FOMC tier + phase codes; bounded tactical columns; sample determinism; standardizer on real tactical/regime stacks (≤5% session clip rate, no created NaNs) | **9/9 PASS** |
| Full suite (Phases 1–5 + Step 12.5) | **191/191 PASS** |

## 5. Known limitations / decisions

1. **Input-1 granularity** — matching events, not raw records (above).
2. **Quiet-gap carry**: tactical/slow rings repeat the last snapshot
   through inactive seconds (state persists; per-second increments are
   exact zeros); gap-fill capped at one ring length. Overnight-quiet GC
   minutes look "frozen" by construction — correct, but models see it.
3. **Bar duration overshoot**: a bar can only close when an event arrives,
   so a quiet gap stretches its duration past the 15 s rule (event count
   never exceeds 64). Median RTH duration respects the rule (tested).
4. **Standardization stats unfitted** by design until Phase 8 (training
   set only); machinery + parity fully tested with synthetic fits.
5. Assembly throughput tracks the Phase 4 pipeline (~8k rows/s); the
   compose-at-sample-instants optimization noted in the Phase 4 report is
   where the win is when Phase 6 fixes the sampling cadence.

## 6. Phase gate

- Step 13 exact-event inputs: **DONE** (granularity decision documented)
- Step 14 adaptive flow bars (64-event / max-duration): **DONE, both rules tested**
- Step 15 1 s tactical (300 steps, plan feature list): **DONE**
- Step 16 10 s slow (180 steps, plan feature list): **DONE**
- Step 17 regime vector (percentile windows + scales + calendar + roll): **DONE**
- `_norm` variants throughout + standardization layer + serialization: **DONE**
- Past-only, deterministic, one code path: **VERIFIED**

**Next per the build order: Phase 6 / Steps 18–19** — future labels
(tradeable-price convention, cost model, sigma_h dual-unit labels,
release exclusion/tagging via the calendar) and the sample index
(uniqueness weights, caps, effective-N report). **Reminder: the macro
release calendar (CPI/NFP/…) must be ingested before Phase 7-style label
hygiene is trustworthy — currently FOMC-only.**
