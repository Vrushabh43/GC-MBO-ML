# Phase 2 Completion Report — Order Lifecycle and Queue Engine

Date: 2026-07-09 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Build Order Step 6

## Verdict: **COMPLETE**

All plan Phase 2 requirements are implemented in the compiled core, inside
the single Phase 1 event loop (Critical Rule 3 — one code path), tested
synthetically and on real data, deterministic, and above throughput targets.
Entry precondition: the Phase 1 gate was resolved on 2026-07-09 by the
user's explicit deferral of the MBP-10 cross-check (out of scope / not yet
purchased — see the Phase 1 report; Phase 1 remains not MBP-10-verified).

---

## 1. What Phase 2 adds

One lifecycle record per order, emitted at its terminal event, plus live
queue queries and the iceberg chain heuristic:

- **Lifecycle record (30 columns)**: add/termination timestamps + prices,
  lifetime, initial/max/final/filled/cancelled size, modify counts
  (size-up / size-down / price-change), terminal state, snapshot flag.
- **Terminal states (neutral mechanics, never intent)**: `filled`
  (fill-removal C), `partial_cancelled`, `cancelled` (trader pull),
  `cleared` (R), `end_of_data`, `replaced` (duplicate-add anomaly). Built on
  the Phase 1 pull-vs-fill-removal classification.
- **Queue engine (Globex FIFO)**: queue position + volume ahead at add,
  FIFO position at termination (free — extracted from the removal walk),
  live `queue_position` / `volume_ahead` / `level_ages` queries. Priority
  rules enforced by the Phase 1 book (decrease keeps, increase loses,
  price change loses) and now measured per order.
- **Distances from market**: from same-side best and from mid, at add and
  at termination (raw fixed-point + tick twins; `_norm` twins are Phase 4).
- **Survival as price approached**: closest same-side-best approach while
  resting, tracked with an O(1)-amortized suffix-extreme deque per
  instrument side + prevailing-value carry per price segment; yields
  `touched_best` and `cancel_before_touch`.
- **CME iceberg refill chains (plan [NEW])**: fill-removal opens a refill
  slot at (side, price); a non-snapshot Add within `iceberg_link_window_ns`
  (default 2 ms) with size ≤ `iceberg_clip_tolerance` × parent clip joins
  the chain (`chain_id` = root order id). **Heuristic per Critical Rule 8:**
  every link stores a confidence in (0,1] (time-decay × clip-match) and
  `link_dt_ns`; chains are never treated as fact. Lifetime diagnostics are
  reported raw AND chain-adjusted.
- **Neutral naming** (plan requirement): `cancel_before_touch_rate`,
  `liquidity_survival_ratio`, `short_lived_large_order_behavior` session
  diagnostics; no intent labels anywhere.

## 2. Files created or changed

| File | Change |
|---|---|
| `core/src/lifecycle.rs` | **new** — tracker: live per-order state, suffix-extreme approach deques, refill-slot map, record emission + running FNV-1a lifecycle digest |
| `core/src/book.rs` | `add`/`remove` return queue position + volume ahead; `modify_order` returns pre-modify state; `best_prices`, `volume_ahead`, `level_ages` |
| `core/src/engine.rs` | tracker hooks in A/C/M/R arms + `finish()` EOD sweep (sorted ids ⇒ deterministic); new counter `f_volume_unattributed` |
| `core/src/lib.rs` | constructor flags (`lifecycle`, `iceberg_window_ns`, `iceberg_clip_tol`); `lifecycle_drain` (raw LE column buffers → numpy), `lifecycle_digest`, `lifecycle_stats`, `volume_ahead`, `level_ages`; state constants |
| `src/queue_engine/lifecycle.py` | **new** — DataFrames from drained columns, chain table, neutral-named session diagnostics, per-session Parquet writer |
| `src/mbo_engine/engine.py` | lifecycle construction from config + drain/digest/stats/queue accessors |
| `src/utilities/config.py` | `LifecycleConfig` loader |
| `config/config.toml` | `[lifecycle]` section (enabled, window 2 ms, clip tol 1.0, output dir) |
| `tests/test_lifecycle_synthetic.py` | **new** — 33 known-answer tests |
| `tests/test_lifecycle_real.py` | **new** — 15 real-data tests |
| `benchmarks/throughput_bench.py` | benchmarks + gates BOTH engine configs |
| `scripts/phase2_lifecycle_demo.py` | **new** — 12-session sweep + deep dive → `reports/phase2_lifecycle.md` |
| `data/processed/lifecycle/` | per-session `lifecycle-YYYYMMDD.parquet` + `chains-YYYYMMDD.parquet` (12 sessions) |

## 3. Tests run and results

| Suite | Result |
|---|---|
| `tests/test_lifecycle_synthetic.py` — 33 tests: terminal states (pull / fill-removal / partial+pull / clear / EOD / replaced / idempotent finish), queue positions at add+termination, Globex priority effects (decrease keeps, increase loses, price move requeues), level ages, distances (same-side, mid, sentinels), closest-approach incl. cross-segment and ask-side symmetry, iceberg chains (link, 3-deep chain, window/price/side/size negatives, pulled-parent, snapshot-add exclusion, confidence values), determinism (replay-twice identical records+digest, digest sensitivity), Phase 1 digest/stats unchanged by lifecycle | **33/33 PASS** |
| `tests/test_lifecycle_real.py` — 15 tests on 2026-01-04: every add terminates exactly once (105,792 = adds+unknown-modifies); fill volume reconciles EXACTLY (Σ filled_size = f_volume − f_volume_unattributed, 1 lot); states reconcile exactly with cancels_pulled / cancels_fill_removal / duplicate_add; fills terminate at queue front >95% (actual 99%+); distances ≥ 0 and closest-approach ≤ distance-at-add with zero violations; diagnostics bounded; chain links respect window and carry confidences; replay-twice lifecycle digest identical; **independent pure-Python FIFO queue replica: queue_pos_at_add matches exactly for all ~89k front-contract adds** | **15/15 PASS** |
| Full suite (Phase 1 + Phase 2) | **81/81 PASS** |
| `benchmarks/throughput_bench.py` (2026-01-06, 4.38M records) | **PASS** — book only: ~4.6M ev/s; book+lifecycle: **~2.9M ev/s (14× the 200k target, 5.8× the burst target)**; both configs replay-twice deterministic; Phase 1 state digest identical with lifecycle on/off |
| 12-session dev-slice sweep (56.3M records) | **ALL CONSERVE** — see `reports/phase2_lifecycle.md` |

## 4. Key empirical findings (real data, dev slice)

1. **FIFO confirmed measurably**: 99.2% of fully-filled front-contract
   orders terminate at queue position 0; the exceptions are multi-order
   sweeps inside one matching event.
2. **Iceberg signature is exactly as the plan warned**: refill-linked clips
   have median lifetime ≈ 4 ms vs ≈ 440 ms for genuine orders — naive
   lifetime statistics would be badly contaminated; chain-adjusted twins are
   reported for every lifetime diagnostic.
3. **Hidden size is large**: chains with 2 displayed / 60 filled
   (executed-to-displayed 30×) exist daily; ~11–19k links/session
   (~0.8% of adds), ~10–17k chains/session.
4. **Stable session behavior across the slice**: fill rate 8–14%,
   cancel-before-touch ≈ 0.31–0.36, liquidity survival @1 tick ≈ 0.88–0.91 —
   plausible, stable inputs for Phase 3 features.
5. Distances at add are ≥ 0 for 100% of front-contract adds in continuous
   trading (median 0 ticks — GC liquidity joins at the touch; p95 ≈ 40
   ticks).

## 5. Known limitations

1. **Queue position is an estimate by construction** (plan wording:
   "estimated from the order's position in the reconstructed FIFO"): CME
   does not disclose true positions; hidden iceberg quantity ahead is not
   visible. Volume-ahead counts displayed lots only.
2. **Chain heuristic is v1**: keyed to exact (side, price) with one slot per
   level (a new full fill at the same level replaces the pending slot);
   confidence = time-decay × clip-match discount. Confidence calibration
   against labeled behavior is future work (Phase 3 iceberg-probability
   feature consumes it as a raw ingredient).
3. **Closest-approach is measured against the same-side best only** (plan's
   "survival as price approached"); measuring against last-trade price is a
   possible Phase 3 extension.
4. **Crossed pre-open books** can produce transiently negative distances;
   values are stored raw (signed) and it is the consumer's job to segment by
   session phase (Phase 3/5).
5. **Snapshot-entered orders** carry the snapshot ts as `ts_added` (true age
   unknown); flagged `from_snapshot` and excluded from add-anchored
   diagnostics.
6. Lifecycle Parquet outputs live on the SATA tier inside the repo (same
   Phase 1 storage deviation; re-point before full-archive runs).

## 6. Phase gate

- Order lifecycle records with all plan fields: **DONE, tested**
- Globex FIFO queue tracking + priority rules: **DONE, tested, independently
  cross-checked (pure-Python FIFO replica, exact match)**
- Iceberg synthetic-parent chains with stored confidence: **DONE, tested**
- Neutral naming: **DONE** (states + diagnostics)
- Determinism (lifecycle replay-twice): **PASS** (CI benchmark + tests)
- Throughput with lifecycle enabled: **PASS** (~2.9M ev/s ≥ 200k/500k)
- Phase 1 behavior untouched (state digest + stats identical): **VERIFIED**

**Phase 3 (core order-flow features, Steps 7–12) is cleared to start**: it
consumes the lifecycle records, live queue/level queries, and chain tables
built here (Aggressive Delta remains T-only per the Phase 1 rule).
