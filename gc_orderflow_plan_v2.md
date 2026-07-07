# GC Futures Order-Flow AI System — Revised Plan (v2.5)

v2.1 amendment: volatility-normalized labels, model targets, and trade
thresholds (Phase 7 subsection "Label and Threshold Normalization", Phase 11
signal rule, Critical Rule 21), so the same configuration works across price
regimes (GC ≈ $1,200 in 2017 through ≈ $4,500 in 2026) without era-specific
recalibration. Cost itself is never normalized.

v2.2 amendment: consolidated **Normalization Architecture** (new Phase 4A) —
three canonical scales (sigma_h for price moves, v_scale for volumes/flow,
d_scale for depth), a serialized input-standardization layer for sequence
models, normalized time-to-extreme labels, the normalize-ingredients-not-
scores rule for composites, and an explicit do-not-normalize list. Pointers
added in Phases 3, 5, 7 and Critical Rule 22.

v2.3 amendment: **Appendix A — Risk Register**. Eleven model-accuracy risks
(reconstruction bugs, leakage, train/serve skew, label noise, effective-N
illusion, regime decay, test-set peeking, multi-task interference, class
imbalance, data quality, latency), each with detection signal, solution
mechanism, and the phase where the defense is built. R1–R3 outrank all
other work.

v2.4 amendment: **data holdings locked in** — GC MBO archive 2017 through
31.03.2026 is on hand. Phase 0 data section replaced with: inventory-audit
requirement, develop-small/scale-later rule, full-archive roll ledger,
April-2026 backfill, MBP-10 verification data, the frozen chronological
split (train 2017–2023 / validate 2024 / test 2025 / final holdout Q1 2026,
COVID stratum tagged), the 12m/3y/full-history training-window experiment,
and the end-to-end retraining-loop rehearsal across 2024–2025. Build-order
Steps 0, 2, 12.5, 20, 22 updated accordingly.

v2.5 amendment: **execution environment locked** — single workstation
(Ryzen 9 3900X 12c/24t, 64 GB RAM, RTX 2080 SUPER 8 GB, NVMe + SATA + slow
network shares). Phase 0 gains a Hardware Profile section with data
format (DBN + zstd), storage placement rules, parallelization strategy
(~10 worker processes, one session per worker), and GPU constraints for
Model C (mixed precision, gradient accumulation, capacity ceiling — which
aligns with the R5 effective-N discipline anyway). This document is the
working specification for implementation in Claude Code.

Instrument: COMEX Gold futures (GC)
Data: Databento, dataset GLBX.MDP3, schema MBO
Language: Python with a compiled performance core (see Phase 0)

This revision preserves the original architecture (MBO reconstruction → order-flow
features → multi-resolution inputs → multi-task quantile model → identical
historical/live pipeline) and adds the amendments that make the plan survivable:
a compiled book engine, tradeable-price labels, purged validation, an economic
calendar, contract-roll rules, a go/no-go gate before deep models, an execution
layer, and live monitoring.

Changes from v1 are marked **[NEW]** or **[CHANGED]**.

---

# PHASE 0 — PERFORMANCE ARCHITECTURE DECISION **[NEW]**

Decide the compute core before writing Phase 1 code. GC produces millions of MBO
events per day with bursts exceeding 50,000 events/second around data releases.
A pure-Python per-event loop (~50–200k events/s) is marginal for bulk historical
processing and will fall behind live exactly when the model matters most.

**Decision (default): Option A — compiled core.**

- Implement the order store, price-level book, and queue engine in Rust
  (via PyO3/maturin) or Cython, exposing a thin Python API.
- Everything downstream (features, aggregation, ML) stays in Python.
- One code path for historical and live, satisfying Critical Rule 3.

Fallback: Option B — use Databento's own book-building utilities for the
aggregated book and reserve custom compiled code for the order-lifecycle/queue
layer only.

Rejected: separate vectorized-historical vs. streaming-live paths. It violates
the same-code-path rule and creates silent train/serve skew.

**Throughput requirements (add to config and CI):**

- Sustained: ≥ 200,000 events/second on historical replay, single core.
- Burst: ≥ 500,000 events/second for ≥ 5 seconds without queue growth.
- Live p99 event-to-feature latency: ≤ 5 ms outside bursts.

**Determinism requirement:** processing the same raw DBN file twice must produce
byte-identical outputs (fixed iteration order, fixed float accumulation order,
no wall-clock dependence). A replay-twice-and-diff test is a required CI test.

**Hardware profile and environment decisions [NEW — v2.5]:**

Target machine (single workstation, Linux x86_64):

- CPU: AMD Ryzen 9 3900X — 12 physical cores / 24 threads, 1 NUMA node.
- RAM: 64 GB (no swap configured).
- GPU: NVIDIA RTX 2080 SUPER, 8 GB VRAM, driver 580.x, CUDA 13.
- Storage:
  - `/home` (sda2): SATA, ~2.4 TB free — CAPACITY tier.

**Data format (final): DBN + zstd (`.dbn.zst`), kept exactly as delivered.**
Rationale: identical decoder for historical and live (Critical Rule 3 / R3
defense); lossless fixed-point int64 prices and native nanosecond
timestamps (no CSV/JSON string round-trip risk); preserves flags
(`F_LAST`), sequence numbers, and order IDs needed for reconciliation and
gap detection (R10); ~4–10× smaller and far faster to decode than CSV/JSON;
read directly via `databento-dbn` (Rust decoder) with transparent zstd
decompression — no decompressed copies ever stored. Preferred layout: one
file per day. MBP-10 verification days (2–3 days each from 2017, 2020,
2023, 2025, Q1 2026) in the same format for the R1 cross-check.

**Storage placement rules:**

- Raw `.dbn.zst` archive: `/home` (capacity tier), immutable, read-only
  permissions after the inventory audit.
- Processed Parquet (1s/10s streams, labels, sample index), model
  artifacts, and the active working set: NVMe fast tier.
- Estimated processed-data footprint for the full archive: ~100–300 GB —
  fits NVMe; monitor via the audit report.
- Nothing hot on CIFS, ever.

**Parallelization strategy (12c/24t, 64 GB):**

- Historical batch processing: session-per-worker process pool, default
  **10 workers** (leave 2 cores for OS/IO), each worker running the full
  deterministic engine on one session — determinism is preserved because
  parallelism is ACROSS sessions, never within one (Critical Rule 4).
- At the Phase 0 per-core target (≥ 200k ev/s) × 10 workers, a full
  2017–2026 archive pass is on the order of **hours, not days** on this
  machine.
- Memory budget: ≤ 4 GB per worker hard cap (streaming design required —
  no full-day materialization of derived per-event data); ~20 GB headroom
  reserved for OS cache, which the NVMe reads will use well.
- Live mode: single pinned core for the hot decode→book→feature path;
  inference and IO on separate threads.

**GPU constraints (RTX 2080 SUPER, 8 GB):**

- Model A (LightGBM): CPU, 24 threads — no GPU needed.
- Models B/C (PyTorch, CUDA): 8 GB VRAM is the binding constraint. Use
  mixed precision (AMP), gradient accumulation for effective batch size,
  and keep Model C's five-branch capacity modest. This ceiling is
  ALIGNED with, not fighting, the R5 effective-N discipline: the model
  size this GPU can train is roughly the model size the effective sample
  count justifies.
- Note: RTX 2080 SUPER is Turing (compute capability 7.5) — no bfloat16;
  use fp16 AMP with gradient scaling. Verify the installed PyTorch build
  supports CC 7.5 under CUDA 13 during Step 0 environment setup.
- Live inference of Model C on this GPU: single-sample latency will be
  ~1–5 ms — negligible against the R11 latency budget.

**Data holdings and processing plan [CHANGED — v2.4]:**

- **Available archive: GC MBO from 2017 through 31.03.2026** (~9 years,
  spanning the $1,200 range era, COVID 2020, consolidation, and the
  2024–2026 bull run to ~$4,500). This regime diversity is exactly what the
  Phase 4A normalization exists for; without sigma_h/v_scale/d_scale the
  archive would be untrainable as one corpus.
- **Inventory audit is the first data task** (Build Order Step 2): file
  completeness per session, gap scan, DBN schema/version consistency check
  across the years (the reader must handle every encoding version present),
  and a size/compression report. No feature work until the audit report
  exists.
- **Develop small, scale later:** Phase 1 development runs against ~2 weeks
  of early-2026 data. Full-history processing happens only after the engine
  passes MBP cross-checks and the throughput benchmark (at 200k ev/s, a
  full-archive pass is a multi-day single-core batch job — acceptable as a
  batch, unacceptable as a debug loop).
- **Roll ledger:** ~50+ contract rolls in the span. The Phase 1 roll policy
  must emit an explicit active-contract ledger (contract, active-from,
  active-to) for the entire archive, verified against volume, BEFORE any
  labeling.
- **Live gap backfill:** the archive ends 31.03.2026; before live operation,
  backfill from Databento up to the present so the final training window,
  warm-up, and champion/challenger retraining have current data. Price this
  top-up early.
- **MBP-10 for verification:** confirm MBP-10 availability alongside the MBO
  archive, or pull sample verification days (several days spread across
  different years) — required for the R1 defense.
- Storage tiers (unchanged):
  1. Raw DBN files: kept as delivered, immutable.
  2. Event-level derived features: stored **only** in windows around sampled
     timestamps (see Phase 6), not for every event.
  3. 1-second and 10-second feature streams: stored fully (Parquet, partitioned
     by date and contract).
  4. Labels and sample index: Parquet.
- Do not persist a full event-resolution feature timeline for the whole
  history; at scale this is terabytes of mostly redundant data.

**Chronological data split (frozen before any training, per R7) [NEW — v2.4]:**

- **Final holdout: 01.01.2026 – 31.03.2026.** Evaluated exactly once, by
  script, after all decisions freeze. Used for nothing else.
- **Test: 2025** — purged/embargoed walk-forward folds.
- **Validation: 2024** — model selection during development.
- **Training: 2017–2023** (window length per the experiment below).
- **March 2020 (COVID)** is retained but tagged as its own regime stratum:
  the stress test for quantile calibration in extreme volatility, evaluated
  separately, never silently mixed.

**Training-window experiment [NEW — v2.4]:** do not assume all nine years
belong in training — old microstructure patterns were generated by different
counterpart algorithms (R6 applies backward too). At the Model A stage,
train three variants: (a) trailing 12 months, (b) trailing 3 years, (c) full
history with uniqueness and optional time-decay weighting. Purged validation
on recent data decides. Prior expectation: trailing 2–3 years wins for the
30s horizon, with older data contributing mainly through normalization
statistics and regime calibration — but this is a hypothesis to test, not a
setting to assume.

**Full retraining-loop rehearsal [NEW — v2.4]:** with nine years on hand,
the R6 production design (monthly retrain on trailing window,
champion/challenger promotion, decay tripwire) must be simulated end-to-end
across 2024–2025 as part of Phase 9 — measuring how the SYSTEM would have
behaved through time, not just how individual models score.

---

# PHASE 1 — RAW MBO DATA ENGINE

(Original content retained; additions marked.)

Build a robust reader for historical and live Databento MBO data. Process all
MBO events correctly: Add, Cancel, Modify, Trade, Fill, book Clear/Reset.

Track every resting order by order ID:

    orders[order_id] = {
        side, price, current_size, initial_size,
        timestamp_added, timestamp_last_updated,
        queue_position, current_state
    }

Handle: partial fills, complete fills, cancels, modifications, price changes,
size changes, book resets, duplicate protection, out-of-order protection,
session boundaries, contract changes, data gaps, reconnection, live snapshot
initialization. Respect Databento/CME event boundaries (`F_LAST`, matching-event
grouping).

**Trade/Fill reconciliation rule [NEW].** Databento MBO reports each execution
from two sides: the aggressor `T` (Trade) action and `F` (Fill) records against
resting orders. To avoid double-counting:

- Aggressive volume, delta, and trade counts are computed **only** from `T`
  actions (using the aggressor side).
- `F` records are used **only** to update resting-order state (size reduction,
  fill attribution, queue depletion).
- A reconciliation check asserts that summed `F` volume per matching event
  equals the corresponding `T` volume; mismatches are logged as data-quality
  incidents.

**Contract-roll policy [NEW].** Define in config:

- Active-contract rule: front month by volume; roll when the next month's daily
  volume exceeds the current front month for 2 consecutive sessions (or a fixed
  calendar rule, e.g. 3 business days before FND — configurable, pick one and
  keep it fixed per experiment).
- At a roll: reset all rolling windows, normalization state, and regime
  percentiles. Never stitch features or labels across contracts.
- Store the contract symbol and days-to-expiry with every sample.
- Optionally process both front and next contract in parallel during the roll
  window for analysis, but train on the active contract only.

**Milestone 1 (unchanged):** reconstruct the full book and show best bid/ask,
spread, top-10 bid and ask levels, total size and order count per level, from
both the individual-order view and the aggregated price-level view.

**Milestone 1 verification [NEW]:** cross-check the reconstructed book against
Databento MBP-10 snapshots for the same timestamps over at least one full
session, including the open, a data release, and the close. Zero tolerance for
top-10 mismatches outside documented feed anomalies.

---

# PHASE 2 — ORDER LIFECYCLE AND QUEUE ENGINE

(Original feature list retained: time added, initial/current/filled/cancelled
size, lifetime, final state, distance from market at add/cancel, filled flag,
survival as price approached, queue position and movement; features for order
lifetime, age, survival, cancel-before-touch, front-queue depletion, back-queue
cancellation, queue turnover, new joins, liquidity age distribution.)

Neutral naming retained: `short_lived_large_order_behavior`,
`cancel_before_touch_rate`, `liquidity_survival_ratio`. Never label behavior as
spoofing.

**Globex queue mechanics [NEW].** GC uses FIFO matching. The queue engine must
model priority correctly:

- Size **decrease** via Modify: priority retained.
- Size **increase** via Modify: priority lost (order goes to back of queue).
- Price change via Modify: priority lost.
- Queue position is estimated from the order's position in the reconstructed
  FIFO at its price level, updated on every add/cancel/fill/modify at that
  level.

**CME iceberg refill handling [NEW].** When an iceberg's displayed portion is
exhausted, the refill arrives as a **new order ID** at the back of the queue.
Naive lifetime statistics therefore see icebergs as chains of short-lived
orders. Add a synthetic-parent heuristic:

- Link an add to a candidate parent when it appears at the same price, same
  side, within a configurable latency window (default ≤ 2 ms) after a full fill
  at that price, with size ≤ the parent's typical displayed clip.
- Linked chains form a `synthetic_order_id` used by lifetime, survival, and
  iceberg-probability features.
- Exclude linked refill chains from short-lifetime/cancel-behavior statistics,
  or report both raw and chain-adjusted versions.
- This is a heuristic; store the link confidence and never treat it as fact
  (consistent with Critical Rule 8).

---

# PHASE 3 — CORE ORDER-FLOW FEATURES

(Original content retained in full: build and independently test, in order —
Aggressive Delta, Replenishment, Price Progress, Absorption, Pulling/Stacking,
Price Impact, MLOFI (levels 1–10 plus near/middle/deep), Liquidity Sweep,
Sweep Failure/Reclaim, Queue Depletion, Order Survival, Iceberg Probability,
Liquidity Vacuum, Book Resiliency, Book Imbalance, Microprice, Trade Burst
Intensity, Queue Turnover, Order Lifetime, Liquidity Age. All features numeric
continuous scores; Booleans only alongside continuous scores. All the original
per-feature sub-lists apply unchanged.)

Additions:

- All feature construction follows the canonical Normalization Architecture
  (Phase 4A): tick/point distances get sigma-normalized twins, contract
  quantities get v_scale/d_scale twins, and composite scores are built from
  normalized ingredients — never re-scaled after construction. **[v2.2]**
- Aggressive Delta and every volume-based feature use the `T`-only rule from
  Phase 1.
- Iceberg Probability consumes the synthetic-parent chains from Phase 2
  (`executed_to_displayed_ratio`, refill count/timing per chain).
- Each feature ships with: a unit test on synthetic event sequences with known
  answers, a property test (bounds, sign conventions), and a one-session visual
  sanity notebook. A feature is "done" only when all three exist.

---

# PHASE 4 — FEATURE NORMALIZATION

(Original content retained: keep raw and normalized versions; comparison
windows of 1m/5m/15m/60m/session; rolling percentiles and robust z-scores;
no future leakage; examples like `current_absorption_percentile_60m`.)

Additions **[NEW]**:

- All rolling normalization state resets at session boundaries and at contract
  rolls (Phase 1 policy).
- Percentile windows never span a roll or a maintenance break.
- Normalization parameters used in training are serialized with the model and
  reused verbatim in live inference (train/serve parity check in tests).

---

# PHASE 4A — NORMALIZATION ARCHITECTURE (CANONICAL) **[NEW — v2.2]**

This section is the single authoritative specification of all normalization
in the system. Phases 3, 4, 5, 7, and 11 defer to it. Purpose: features,
targets, and thresholds must be stationary across price eras (GC $1,200 →
$4,500), across sessions (Asia overnight vs. NY open), and across volatility
regimes — while quantities that are physical facts stay raw.

## The three canonical scales

All three are rolling, strictly past-only, robust (median-based), reset at
session boundaries and contract rolls, computed identically in historical
and live mode, and stored alongside the data they normalize.

    sigma_h(t)  — price-move scale, per horizon h [defined in Phase 7 /
                  v2.1]: median absolute h-horizon mid move, trailing 60 min.
                  Normalizes: labels, model movement targets, trade
                  thresholds, and all tick/point-denominated feature
                  distances.

    v_scale(t)  — flow scale: rolling median contracts-per-second, trailing
                  60 min. Normalizes contract-denominated flow: buy/sell
                  volume, delta, CVD increments, replenished volume, sweep
                  volume, stack/pull volumes, iceberg executed volume,
                  trade sizes, event/trade rates.

    d_scale(t)  — depth scale: rolling median aggregate top-5-level depth
                  (per side or combined; configurable, default combined),
                  trailing 60 min. Normalizes book-depth quantities:
                  bid/ask depth at L1/L3/L5/L10, vacuum depth inputs,
                  depletion volumes, resiliency depth-recovery amounts.

Window lengths (default 60 min) are configurable per scale. Each scale is
also emitted as a feature in the regime vector (raw and percentile), since
the scale level itself is informative.

## What gets normalized where

1. **Tick/point distances → sigma-normalized twin.** price_dist_from_mid
   (Input 1), sweep_distance_ticks, microprice_displacement, price
   progress features, distance-from-market at add/cancel (Phase 2),
   price_move in flow bars, vacuum lookahead distances. Store raw and
   `_norm`; models consume `_norm`.
2. **Contract quantities → v_scale/d_scale-normalized twin.** As listed
   above. Store raw and `_norm`; models consume `_norm`. Phase 4
   percentiles are kept IN ADDITION — percentiles capture rank, scale
   ratios capture magnitude; both are needed (a 99th-percentile burst at
   2× normal and at 20× normal must be distinguishable).
3. **Composite scores → normalize the INGREDIENTS, not the score.**
   Absorption, impact efficiency, sweep intensity, depletion ratio, iceberg
   probability etc. are constructed FROM already-normalized volume/depth/
   distance inputs. Never re-scale a bounded score after construction;
   post-hoc scaling of bounded outputs hides era bias instead of removing
   it. (Unit tests must verify score invariance under a synthetic 3×
   volume/vol scaling of identical relative behavior.)
4. **Time-to-extreme labels → fraction of horizon.**
   `time_to_high_h_frac = time_to_high_h / h`, bounded [0,1], comparable
   across horizons; the multi-task timing loss uses the fractional form so
   no horizon dominates by unit scale. Raw seconds retained as metadata.
5. **Movement labels, targets, thresholds → sigma_h** (v2.1, Phase 7 /
   Phase 11; unchanged).

## Input-standardization layer for sequence models (Phase 5/8)

Separate from era-stationarity: raw scales are numerically hostile to
neural training. Before tensors reach any sequence model:

- **Log transforms** for heavy-tailed inputs: inter-event times
  (`log1p`, in microseconds), order/trade sizes, event counts.
- **Robust per-feature standardization**: (x − median) / MAD, statistics
  computed on the TRAINING SET ONLY, serialized inside the model artifact
  bundle, applied byte-identically at inference. Covered by the
  train/serve parity test (Engineering Requirements).
- **Winsorization**: clip standardized inputs to a configurable bound
  (default ±8) so single outlier events cannot destabilize a live forward
  pass. Clipping counts are logged as a drift signal (Phase 10
  monitoring).
- Applies to all five model inputs. Model A (gradient boosting) does not
  need standardization for optimization but DOES use the era-normalized
  `_norm` feature variants as its primary feature set, with raw values
  retained for debugging and ablation.

## Do-NOT-normalize list (authoritative)

- **Trading cost** — physical fact in ticks/dollars (Critical Rule 21).
- **Already-bounded/dimensionless outputs** — absorption scores, survival
  ratios, replenishment_ratio, imbalance (−1..+1), iceberg probabilities,
  sweep_failure_score, all percentile features. Normalizing these again is
  double normalization.
- **Spread in ticks** — quasi-stationary at ~1 tick; keep raw; regime
  widening is captured by the spread percentile feature and the
  live-spread cost model.
- **Categoricals and calendar features** — event type, side, session
  phase, event tier, seconds-to-release (log-standardized only in the NN
  input layer, not era-normalized).
- **Probabilities and quantile coverage targets.**

## Verification requirements

- Era-invariance test: synthetic replay of the same relative event pattern
  at 1× and 3× price/volume scale must yield matching `_norm` features and
  composite scores within tolerance.
- All three scales enter the train/serve parity test: historical pipeline
  and live pipeline in replay mode must produce identical sigma_h, v_scale,
  d_scale for the same raw data.
- Every serialized model bundle records: scale definitions + window
  configs + input-standardization statistics + clipping bounds.

---

# PHASE 4.5 — ECONOMIC CALENDAR AND EVENT-RISK MODULE **[NEW]**

A microstructure model cannot predict scheduled macro releases, and release
moves contaminate labels. Add a calendar module before labeling:

- Ingest a scheduled-release calendar (CPI, PPI, NFP, FOMC statements/minutes,
  PCE, GDP, Treasury auctions relevant to gold, plus COMEX settlement window
  and Globex open/close/maintenance times). Source is configurable (CSV import
  or API); calendar data is versioned with the dataset.
- Features (past-only, no leakage): `seconds_to_next_scheduled_event`,
  `seconds_since_last_scheduled_event`, event-tier encoding (high/medium/low
  impact), session phase (Asia/London/NY, settlement, close).
- Label hygiene: any sample whose **label window** overlaps a high-impact
  release is either (a) excluded from training, or (b) tagged
  `release_contaminated=1` and trained/evaluated as a separate stratum.
  Default: exclude for high-impact, tag for medium. Configurable.
- Live behavior: predictions inside a configurable pre/post-release blackout
  (default: 2 minutes before to 5 minutes after high-impact releases) are
  suppressed or emitted with an explicit `event_risk` warning flag.

---

# PHASE 5 — FINAL MODEL INPUT ARCHITECTURE

(Original five-input hybrid design retained unchanged:)

1. **Exact MBO event stream** — last 1,024 events × ~20–30 event features
   (type, side, size, price distance from mid, inter-event time, spread, order
   age, queue position, book imbalance, microprice displacement), exact order
   preserved.
2. **Adaptive flow bars** — ~64 meaningful events per bar with a max-duration
   rule; last 256 bars × ~40–60 features (duration, event counts by type,
   buy/sell volume, delta, add/cancel volume by side, replenishment, price
   movement, MLOFI, absorption, sweep activity, queue changes, intensity).
3. **Tactical clock-time context** — last 5 minutes at 1-second resolution
   (300 steps): delta, CVD change, absorption, MLOFI, price impact, sweeps,
   sweep failure, queue depletion, survival, replenishment, resiliency,
   vacuum, trade intensity.
4. **Slow context** — last 30 minutes at 10-second resolution (180 steps):
   return, volatility, total delta, CVD slope, average absorption, failed-sweep
   count, MLOFI trend, depth trend, spread, trade activity, resiliency,
   directional efficiency.
5. **Regime vector** — compact 1m/5m/15m/60m/session percentile summary of
   event rate, buy/sell volume, delta, volatility, spread, absorption, depth,
   queue turnover, replenishment, sweep activity.

Additions **[NEW]**:

- Regime vector also includes the calendar features from Phase 4.5 and
  days-to-expiry / roll-proximity from Phase 1.
- All five inputs carry the era-normalized (`_norm`) feature variants per
  Phase 4A; sequence-model tensors additionally pass through the Phase 4A
  input-standardization layer (log transforms, robust z-score serialized
  with the model, winsorization at ±8). The regime vector includes sigma_h,
  v_scale, and d_scale themselves (raw + percentile). **[v2.2]**
- 1,024 / 64 / 256 remain configurable starting values, calibrated later
  against GC event-rate distributions (unchanged from v1).

---

# PHASE 6 — TRAINING SAMPLE CREATION

(Original design retained: one continuous feature timeline + separate sample
index; ~1 sample/second in active periods; event-triggered extra samples on
sweeps, absorption onset, MLOFI shifts, trade bursts, queue collapse, vacuum,
major replenishment; minimum-spacing rule.)

**[CHANGED] Overlap is treated as a first-class problem, not a footnote:**

- With 1 Hz sampling and 2m/10m label windows, adjacent samples share >99% of
  their label horizon. Row count ≠ information count.
- Compute and report an **effective sample size** estimate (e.g., via label
  autocorrelation or average-uniqueness weighting à la López de Prado) for
  every training set.
- Optionally weight samples by label uniqueness during training
  (configurable; test both).
- Event-triggered samples are capped per event type per session to prevent one
  violent session dominating the set.
- Samples whose label windows cross session boundaries, rolls, or high-impact
  releases follow the Phase 4.5 exclusion/tagging rules.

Storage note: per Phase 0, event-level input windows (Input 1) are materialized
only for indexed samples; clock-time streams are stored continuously.

---

# PHASE 7 — MODEL OUTPUTS AND LABELS

Multi-task outputs retained: per-horizon direction probabilities
(bullish/bearish/no-trade), max upside, max downside, final return, time to
high/low, with quantile regression (Q50/Q75/Q90) for max upside and downside.
Reporting must remain probabilistic ranges, never "price will move exactly X."

**[CHANGED] Horizon priorities:**

- **30 seconds — primary.** Order-flow signal lives here.
- **2 minutes — secondary.**
- **10 minutes — experimental.** At this horizon GC is dominated by macro flow
  not present in the book. Keep the head, weight it low in the multi-task loss
  (suggested initial weights 0.5 / 0.35 / 0.15), and expect it may show no
  edge. Do not let the 10m head drive architecture decisions.

**[NEW] Labels are defined on tradeable prices, not mid:**

- Entry reference at prediction time t: the aggressive side — buy at best ask,
  sell at best bid.
- For a hypothetical short (bearish label): downside is measured from entry
  (best bid at t) to the best **bid** path minimum in the horizon (the price at
  which the position could realistically be covered is the ask; use
  configurable convention, default: mark exits at the touch of the opposite
  side). For a long, mirror.
- Simpler configurable alternative: mid-based labels minus an explicit cost
  adjustment. Default cost model for GC: spread (≈1 tick = 0.1 pt = $10) +
  slippage 0.5–1 tick + fees, i.e. **1.5–2.5 ticks round trip**, configurable.
- Whichever convention is chosen, it is stored in the label metadata and used
  identically in evaluation and the Phase 11 policy layer.
- Direction classes (bullish/bearish/no-trade) are defined by whether the
  cost-adjusted favorable move exceeds a configurable multiple of the
  cost-adjusted adverse move — so "no-trade" genuinely means "not worth
  trading after costs," not just "small move." **Thresholds for direction
  classes are stated in volatility units, not fixed points (see the
  normalization subsection below); this prevents low-price/low-vol eras
  (e.g., GC ≈ $1,200 in 2017) from being labeled almost entirely NO_TRADE
  by thresholds calibrated in a high-price/high-vol era.**

## Label and Threshold Normalization **[NEW — v2.1]**

Cost is a physical fact in ticks and dollars (GC tick = 0.1 pt = $10; spread
≈ 1 tick; fees fixed dollars) and **must never be scaled by price level or
volatility.** What scales across eras is move size: point volatility roughly
tracks the price level (percentage volatility is the quasi-stable quantity).
A round trip cost ~0.2 pts both in 2017 (GC ≈ $1,200) and in 2026
(GC ≈ $4,500), but typical 2-minute moves grew from ~0.5–1.0 pts to ~2–4 pts.
Therefore: normalize the **labels, model targets, and thresholds** by recent
realized volatility — not the cost.

**Volatility scale definition.** Per horizon h ∈ {30s, 2m, 10m}:

    sigma_h(t) = rolling scale of h-horizon moves over the trailing window
                 (default: median absolute h-horizon mid move over the past
                 60 minutes, in points; window configurable)

- Computed strictly past-only (no leakage).
- Resets at session boundaries and contract rolls (Phase 1/4 rules).
- A robust estimator (median absolute move or trimmed quantile) is required;
  plain standard deviation is too release-sensitive.
- sigma_h(t) is stored as label metadata for every sample.

**Dual-unit labels.** Every movement label is stored in both units:

    future_downside_2m_pts  = 5.7
    future_downside_2m_norm = future_downside_2m_pts / sigma_2m(t)

Same for upside, final return, and per-horizon adverse-move quantitities.
Time-to-extreme labels are additionally stored as fraction of horizon
(`time_to_high_h_frac = time_to_high_h / h`, bounded [0,1]) and the timing
heads train on the fractional form (Phase 4A). **[v2.2]**

**Model targets are the normalized versions.** Quantile and return heads are
trained on `_norm` labels so that a "2-sigma setup" in 2017 and 2026 is the
same target and learned patterns transfer across price regimes. Training on
raw points teaches the model the calendar, not the market.

**De-normalization at inference.** Predicted points =
`predicted_norm × sigma_h(now)`, using the live, past-only sigma. The
prediction display reports both units.

**Direction-class thresholds in vol units.** BULLISH/BEARISH/NO_TRADE label
assignment uses thresholds expressed as multiples of sigma_h(t) (with cost
converted to vol units at that timestamp: `cost_norm = cost_pts /
sigma_h(t)`), so class balance is comparable across eras and sessions.

**Explicit non-goal.** Normalization must NOT make every era equally
tradeable. In genuinely low-vol conditions (e.g., quiet 2017 overnight
sessions where cost ≈ 80% of a typical move), the correct output IS
NO_TRADE at high frequency. The purpose of normalization is that NO_TRADE
occurs because the economics are genuinely bad — never because a threshold
was calibrated in the wrong era's point units.

**[NEW] Quantile monotonicity:** predict Q50 plus non-negative increments
(softplus-parameterized) for Q75 and Q90, or sort quantiles post-hoc. Q75 < Q50
must be impossible in emitted output. Quantile calibration (pinball loss and
empirical coverage) is evaluated **per regime** (volatility bucket, event-rate
bucket, session), not only globally.

---

# PHASE 8 — MODEL ARCHITECTURE

Order retained: Model A (LightGBM/XGBoost/MLP on engineered features) →
Model B (single simple sequence model: 1D CNN/TCN/GRU/small Transformer) →
Model C (five-branch encoders + fusion + multi-task heads). Benchmark honestly;
do not assume Transformers win.

**[NEW] GO/NO-GO GATE after Model A — the most important checkpoint in the
project:**

Train Model A on engineered features for the **30-second horizon** only.
Evaluate on a purged out-of-sample period (Phase 9 rules). Proceed to Models
B/C **only if** pre-registered criteria are met, for example:

- Top-decile-confidence predictions show positive expectancy after the
  configured cost model, out of sample, over ≥ 20 trading days; and
- Direction AUC meaningfully above chance with calibration (Brier) better than
  the unconditional baseline; and
- The edge is not concentrated in a single session or single release day.

If the gate fails: iterate on features, labels, and sampling — do **not**
escalate to the multi-branch model. Sequence models amplify existing signal;
they do not create it. Record the gate criteria in config before training so
they cannot be moved after seeing results.

**[NEW] Required Model A deliverables:** feature importance (gain +
permutation), per-feature-family ablations (drop each of the 20 families,
measure degradation), and SHAP or equivalent on the top features. Only feature
families that carry demonstrated signal earn dedicated encoder capacity in
Model C.

---

# PHASE 9 — TRAINING AND VALIDATION

(Original metrics retained: direction — accuracy, balanced accuracy, precision,
recall, F1, ROC-AUC, calibration, Brier; movement — MAE, RMSE, quantile loss,
quantile coverage, range calibration; trading usefulness — by confidence
bucket, session, volatility regime, event-rate regime, after costs/slippage.)

**[CHANGED] Splitting: purged and embargoed, not merely chronological:**

- Chronological ordering of train → validation → test is necessary but not
  sufficient.
- **Purging:** remove from training every sample whose label window overlaps
  the validation/test period.
- **Embargo:** additionally exclude a buffer of at least one full maximum label
  horizon (10 minutes) — recommended one full session — after the training
  period before validation begins.
- Final evaluation: walk-forward with purging/embargo across multiple folds
  (combinatorial purged CV optional for robustness), never a single split.
- Report effective sample sizes (Phase 6) alongside every metric.
- Never random row-level shuffling (unchanged).

**[NEW] Additional required analyses:**

- Performance stratified by proximity to scheduled releases (Phase 4.5 tags).
- Performance in roll weeks vs. normal weeks.
- Stability of feature importances across folds (unstable importances are a
  leakage/overfit red flag).

---

# PHASE 10 — LIVE SYSTEM

(Original pipeline retained: Databento Live MBO → MBO state engine → book
reconstruction → feature engine → exact-event buffer → adaptive-bar buffer →
1s buffer → 10s buffer → regime vector → inference → prediction output, with
the original prediction display format.)

**[NEW] Live safeguards and monitoring:**

- **Snapshot initialization + warm-up:** after connecting, the system emits no
  predictions until all buffers are full and normalization windows are warmed
  (≥ 60 minutes) — mirroring training-time conditions exactly.
- **Book validation:** periodically cross-check the reconstructed top-10
  against MBP snapshots; divergence triggers resync and an incident log.
- **Input drift monitoring:** every model input is compared against its
  training distribution (percentile bounds); out-of-distribution inputs raise
  a flag on the emitted prediction; persistent drift triggers an alert.
- **Latency/gap kill switch:** if event-processing lag, sequence gaps, or
  reconnect storms exceed thresholds, prediction output is suppressed until
  the book is re-verified.
- **Calendar blackouts:** Phase 4.5 release blackouts apply live.
- **Full prediction audit log:** every emitted prediction stores a hash of its
  exact inputs plus model version, so any live output can be byte-reproduced
  in historical replay (this is the operational test of Critical Rule 3).

The prediction display additionally shows: model version, warm-up status,
drift/event-risk flags, and the cost-adjusted interpretation of quantiles.

---

# PHASE 11 — EXECUTION AND POLICY LAYER **[NEW]**

Predictions are not a strategy. Add a minimal but explicit policy layer so
"trading usefulness" in Phase 9 has a defined meaning:

- **Signal rule (v1) — volatility-normalized [CHANGED — v2.1]:** thresholds
  are defined in vol units so one config works across price/vol eras; cost
  stays in raw points (never scaled). Fire when ALL of:
  1. Direction probability: `P(direction) ≥ prob_threshold`
     (default 0.75).
  2. Cost-adjusted asymmetry:
     `Q50_favorable_pts − cost_pts > k × Q75_adverse_pts`
     (equivalently in normalized space:
     `Q50_fav_norm − cost_pts/sigma_h(now) > k × Q75_adv_norm`;
     k configurable, default 1.5). Where the live-spread cost model is
     enabled, `cost_pts` uses the observed current spread, not the assumed
     one.
  3. Tradeability gate:
     `cost_pts / sigma_h(now) < c_max`
     (default c_max = 0.35). When a round trip costs more than ~a third of a
     typical h-horizon move, no prediction strength justifies entry; this
     gate correctly suppresses trading in very low-vol conditions (e.g.,
     quiet low-price-era overnight sessions) without any era-specific
     recalibration.
  4. Safety gates: warm-up complete, no drift flag, no calendar blackout,
     book validated, not inside a roll transition.

- **Position management (v1):** fixed size 1 contract; exit at horizon end, or
  at predicted-quantile-derived stop/target; no pyramiding.
- **Fill simulation — pessimistic by default:** market entries pay the full
  spread plus configured slippage; if simulating passive entries, use a
  queue-position-aware fill model (fill only if the level trades through your
  estimated queue position) — this is where the Phase 2 queue engine pays off.
- **Costs:** the same cost model as Phase 7 labels, applied consistently.
- Evaluate: expectancy, hit rate, profit factor, max drawdown, turnover, and
  sensitivity to threshold and cost assumptions. Report per session,
  volatility regime, and event-rate regime.
- Anything more sophisticated (sizing from quantiles, adaptive thresholds,
  multi-signal netting) comes only after v1 is measured.

---

# ENGINEERING REQUIREMENTS

(Original retained: clean modular structure, type hints, dataclasses, config
files over hardcoding, structured logging, unit + integration tests,
reproducible seeds, Parquet for processed data, streaming/memory-aware
processing, no full-dataset loads, efficient rolling windows.)

**[CHANGED] Project structure — additions marked:**

    gc_orderflow_ai/
    │
    ├── config/
    ├── data/
    │   ├── raw_mbo/
    │   ├── processed/
    │   ├── features/
    │   ├── labels/
    │   ├── calendar/            # [NEW] versioned economic calendar data
    │   └── sample_index/
    │
    ├── core/                    # [NEW] Rust or Cython performance core
    │   ├── order_store/
    │   ├── book/
    │   └── queue/
    │
    ├── src/
    │   ├── databento_io/
    │   ├── mbo_engine/          # thin Python wrapper over core/
    │   ├── order_book/
    │   ├── queue_engine/
    │   ├── calendar/            # [NEW] release calendar + blackout logic
    │   ├── features/
    │   ├── aggregation/
    │   ├── labeling/
    │   ├── datasets/
    │   ├── models/
    │   ├── training/
    │   ├── evaluation/
    │   ├── policy/              # [NEW] Phase 11 execution/policy layer
    │   ├── live/
    │   │   └── monitoring/      # [NEW] drift, latency, book-validation, audit
    │   └── utilities/
    │
    ├── tests/
    ├── benchmarks/              # [NEW] throughput + determinism CI benchmarks
    ├── notebooks/
    ├── scripts/
    └── README.md

**[NEW] Additional engineering rules:**

- Determinism CI test: process a fixed raw file twice, byte-compare all
  outputs.
- Throughput CI benchmark against the Phase 0 targets.
- Model artifacts bundle: weights + normalization state + feature list +
  label convention + cost model + git hash, versioned together.
- Train/serve parity test: run the historical pipeline and the live pipeline
  (in replay mode) over the same raw data; feature vectors must match exactly.

---

# CRITICAL RULES

(Rules 1–15 retained verbatim from v1.) Additions:

16. Define labels on tradeable prices or explicitly cost-adjusted mid; store
    the convention with the labels.
17. Use purged and embargoed splits; report effective sample sizes.
18. Never train or evaluate across contract-roll or high-impact-release
    contamination without explicit tagging.
19. Do not proceed past the Model A gate on failed pre-registered criteria.
20. Every live prediction must be byte-reproducible from logged inputs.
21. Movement labels, model targets, and trade thresholds are volatility-
    normalized (sigma_h, past-only); trading cost is never scaled — it stays
    in raw ticks/points. Store sigma_h(t) with every label. **[v2.1]**
22. Phase 4A is the single authoritative normalization spec: three scales
    (sigma_h, v_scale, d_scale), ingredients-not-scores for composites,
    serialized input standardization, and the do-not-normalize list. No
    feature or label may introduce ad-hoc normalization outside it. **[v2.2]**

---

# REQUIRED BUILD ORDER (REVISED)

Step 0: **[NEW]** Choose and scaffold the compiled core (Phase 0); set
throughput/determinism CI. Data is on hand (2017–31.03.2026
archive); price the April-2026-to-present backfill and confirm
MBP-10 verification data. **[CHANGED — v2.4]** Environment
verification on the target workstation: Rust toolchain +
maturin, `databento`/`databento-dbn` packages, PyTorch with
CUDA support for compute capability 7.5, storage placement per
the v2.5 Hardware Profile (raw archive on /home capacity tier,
processed data on NVMe, nothing on CIFS), and record the
archive's actual path and `du -sh` size in config. **[v2.5]**
Step 1: Project structure and configuration.
Step 2: **Inventory-audit the full archive** (completeness, gaps, DBN
schema versions, sizes), then select the ~2-week early-2026
development slice. **[CHANGED — v2.4]**
Step 3: Build the individual-order store (in core/).
Step 4: Build full order-book reconstruction (in core/), including the
Trade/Fill reconciliation rule.
Step 5: Verify top-of-book and top-10 levels **against MBP snapshots** for a
full session. **[CHANGED]**
Step 6: Build order lifecycle and queue tracking, including Globex priority
rules and iceberg-chain heuristic. **[CHANGED]**
Step 7: Build Aggressive Delta (T-only).
Step 8: Build Replenishment.
Step 9: Build Price Progress.
Step 10: Combine into the first Absorption detector.
Step 11: Test Absorption visually and numerically.
Step 12: Add remaining order-flow features one by one (unit + property +
visual test each).
Step 12.5: **[NEW]** Build the economic-calendar module and roll policy;
emit and verify the full-archive active-contract ledger.
**[CHANGED — v2.4]**
Step 13: Build exact-event inputs.
Step 14: Build adaptive flow bars.
Step 15: Build 1-second tactical features.
Step 16: Build 10-second slow features.
Step 17: Build the regime vector (including calendar/roll features).
Step 18: Create future labels (tradeable-price convention, cost model,
release exclusion/tagging). **[CHANGED]**
Step 19: Create the sample index (uniqueness weights, caps, effective-N
report). **[CHANGED]**
Step 20: Train Model A baseline (30s horizon) → **GO/NO-GO GATE** with
pre-registered criteria, feature importance, ablations, and the
three training-window variants (12m / 3y / full history) from
Phase 0. **[CHANGED — v2.4]**
Step 21: (Gate passed) Train Model B, then the multi-branch Model C.
Step 22: Evaluate with purged walk-forward folds, required stratified
analyses, and the full retraining-loop rehearsal across
2024–2025. Final holdout (Q1 2026) evaluated once, last.
**[CHANGED — v2.4]**
Step 23: Build historical replay + train/serve parity test.
Step 23.5: **[NEW]** Build the Phase 11 policy layer and cost-sensitive
backtest.
Step 24: Connect the identical pipeline to Databento Live MBO with warm-up,
monitoring, blackouts, and audit logging. **[CHANGED]**

Do not attempt to generate the whole project in one uncontrolled step. At each
phase: explain what is being built, show the file structure, write complete
working code, write tests, show how to run it, verify output — only then move
on.

---

# EXPECTATION SETTING (HONEST FRAMING) **[NEW]**

This system competes at short horizons with colocated market makers. The
realistic best case is a well-calibrated short-horizon context model — useful
for execution timing, risk framing, and possibly a modest standalone edge in
the 30-second/2-minute horizons — not a money printer. The plan's own emphasis
on probability ranges, cost-adjusted labels, and a hard baseline gate exists to
find out cheaply and honestly whether the edge is real.

---

# APPENDIX A — RISK REGISTER **[NEW — v2.3]**

Each entry: the risk, its detection signal, the solution mechanism, and the
phase where the defense is BUILT (not merely intended). Guiding principle:
every defense is an automated test, a structural constraint that makes the
error impossible, or a measured quantity fed back into the design — never
"be careful." Operating mindset: the pipeline is guilty until proven
innocent; suspiciously good results are investigated as probable bugs first.

## R1 — Silent book-reconstruction bugs (severity: critical)

Corrupts every downstream feature and label; the model learns the bug and
validation metrics stay plausible.

- Detection: MBP-10 cross-check mismatches; engine invariant violations.
- Solution (Phase 1): (a) continuous MBP-10 cross-check on EVERY processed
  session, mismatch count tracked and required = 0; (b) always-on engine
  invariants (bid < ask, no negative sizes, order-store totals == level
  aggregates, F-volume reconciles with T-volume per matching event) that
  halt on violation and dump the event window; (c) synthetic-exchange test
  harness generating known sequences (priority-losing modifies, partial
  fills, resets) with book state asserted by construction.

## R2 — Subtle future leakage (severity: critical)

Symptom is results that look TOO GOOD. Centered windows, full-dataset
normalization stats, bar-close timestamp slips, sigma computed with a
future tick.

- Detection: shuffled-future test failures; the too-good alarm.
- Solution (Phases 3–4): (a) structural — a single `FeatureClock` data-access
  abstraction; features can only read timestamps ≤ clock, no other path
  exists; (b) shuffled-future CI test — replace all post-sample data with a
  random other day, recompute; any changed feature value read the future;
  (c) too-good alarm — configurable threshold (e.g., 30s direction AUC >
  0.60) triggering mandatory leakage audit before results are acted on.

## R3 — Train/serve skew (severity: critical)

Accurate in backtest, degraded live; inputs at inference distributionally
shifted from training.

- Detection: parity-test diffs; live input-drift flags (Phase 10).
- Solution (Phases 0/10): CI parity job — same raw DBN through historical
  batch pipeline and live pipeline in replay mode; byte-compare all feature
  vectors, all scales (sigma_h, v_scale, d_scale), all input tensors.
  Warm-up parity: live cold-start behavior (snapshot seed, buffer fill,
  60-min warm-up) replicated at every historical session start. Must pass
  before Phase 10 touches real capital.

## R4 — Label noise at short horizons (severity: high, irreducible)

True predictable component is a small fraction of move variance; ceiling is
low and noise invites overfit.

- Solution (Phases 7–9): design around it, don't fight it — quantile
  regression models the distribution; uniqueness weighting stops redundant
  noisy samples dominating; evaluation centers on CALIBRATION and
  top-confidence-bucket economics, not overall accuracy; gate criteria set
  expectations so a genuinely working low-R² model isn't killed.

## R5 — Effective-sample-size illusion (severity: high)

Millions of 99%-overlapping rows ≈ tens of thousands of independent samples;
deep models memorize the correlation structure.

- Detection: effective-N report vs. row count; big-model wins that vanish
  on the final holdout.
- Solution (Phases 6/8): compute uniqueness weights at sample-index
  creation; report effective N with every metric; weight training loss by
  uniqueness; size model capacity to effective N (dropout, small embeddings,
  early stopping on purged validation). Distrust any capacity-driven gain
  until confirmed on the untouched holdout.

## R6 — Regime drift and alpha decay (severity: high, inevitable)

The training market is not the trading market; microstructure edges decay
over months as counterpart algorithms adapt.

- Detection: live rolling calibration (Brier, quantile coverage) and
  top-bucket expectancy outside backtest confidence intervals.
- Solution (Phases 9–10): (a) walk-forward retraining as the PRODUCTION
  design (e.g., monthly on trailing 6–12 months); (b) champion/challenger
  promotion on pre-set criteria over recent purged data; (c) decay tripwire
  — N consecutive days outside the interval auto-de-risks (raise
  thresholds or halt) until a retrain restores performance.

## R7 — Test-set contamination by repeated peeking (severity: high, human)

Thirty evaluate-tweak-evaluate cycles make the "out-of-sample" number
quietly in-sample.

- Solution (Phases 8–9): three-tier split — development (free), validation
  (walk-forward selection), FINAL HOLDOUT evaluated once by script after
  decisions freeze, results logged immutably. Gate criteria committed to
  git before Model A trains. Every test-set evaluation logged with
  timestamp and reason; a long log = a fictional out-of-sample number.

## R8 — Multi-task interference (severity: medium)

Noisy heads (10m direction, timing) degrade learnable heads (30s) through
shared gradients.

- Detection: per-head validation metrics logged every epoch; multi-task
  30s head underperforming a 30s-only reference model.
- Solution (Phase 8): train the 30s-only reference first; the multi-task
  model must match or beat it on the 30s head, else remove heads in order
  of noisiness (10m timing, then 10m direction). Escalate to gradient-
  conflict mitigation (per-head loss normalization, PCGrad) only on
  evidence.

## R9 — NO_TRADE class imbalance and calibration skew (severity: medium)

Model collapses to NO_TRADE for cheap accuracy; event-boosted training mix
distorts the probabilities the policy layer depends on.

- Detection: reliability diagrams per session/vol regime; per-class
  precision vs. headline accuracy.
- Solution (Phases 8–9): inverse-class-frequency loss weighting; post-hoc
  calibration layer (isotonic or temperature, per class per horizon) fitted
  on a purged validation slice sampled at UNIFORM clock frequency — not the
  event-boosted mix; headline direction metric = top-bucket per-class
  precision.

## R10 — Data-quality landmines (severity: medium)

Feed gaps, crossed books, holiday half-sessions, maintenance windows —
either crash visibly (fine) or corrupt features silently (dangerous).

- Detection: data-quality module flags (sequence gaps, timestamp jumps,
  impossible book states) producing a per-timestamp quality mask.
- Solution (Phases 1/10): QUARANTINE, never patch — flagged windows
  excluded from warm-up, samples, and labels, plus a buffer equal to the
  longest rolling window (60 min) after the anomaly since rolling state is
  contaminated. Missing training data is harmless; corrupted data is not.
  Live: quality flag suppresses predictions until buffers re-warm
  (extension of the Phase 10 kill switch).

## R11 — Latency consuming the edge (severity: medium)

Labels assume action at prediction time; reality is 50–500 ms of feed +
compute + transit, material at 30-second horizons.

- Detection: measured end-to-end latency distribution (feed ts → book →
  features → inference → order ack), instrumented from day one.
- Solution (Phases 7/10/11): set `action_delay` in config to ~p75 of the
  measured distribution and redefine labels to start at t + action_delay —
  the model trains on the move that is actually capturable. Re-run labeling
  with the measured delay; an edge that dies here dies cheaply in Phase 7,
  not in production. Keep the inference path fast (compiled core,
  pre-allocated buffers) so the priced-in delay stays small.

## Priority ordering

R1, R2, R3 outrank everything: they corrupt measurements and produce FALSE
CONFIDENCE rather than visible failure. R4–R7 make the model worse but
detectably so. R8–R11 are managed operational risks. Resource conflicts
resolve in that order.

---

Start with Phase 0 + Phase 1 only. Do not start feature engineering or model
training until the order-book engine is verified against MBP snapshots and
passes the determinism and throughput benchmarks.
