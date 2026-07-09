# Phase 1 Completion Report — Raw MBO Data Engine

Date: 2026-07-09 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Build Order Steps 0–5

## Verdict: **COMPLETE, with the MBP-10 snapshot verification explicitly DEFERRED**

Everything implementable with owned data is implemented, tested, and passing.
The single open item was the **MBP-10 cross-check** (plan "Milestone 1
verification"), which requires MBP-10 data we do not own. An independent
pure-Python reference-book cross-check was substituted in the interim.

> **Decision (user, 2026-07-09): MBP-10 cross-check DEFERRED.** MBP-10
> snapshot data will not be purchased or tested in this phase. The check is
> reclassified as an **out-of-scope / not-yet-purchased validation item** and
> does **not** block Phase 2. The pure-Python independent reference-book
> cross-check (exact top-10 agreement, both sides) plus the existing Phase 1
> test suite are accepted as the **interim R1 defense**.
>
> **Caveat, stated plainly: Phase 1 is NOT MBP-10 snapshot-verified.** The
> book reconstruction has never been compared against an exchange-derived
> MBP-10 feed. The interim cross-check is an independent *re-implementation*
> of the same MBO semantics, so it defends against implementation bugs but
> not against a shared misreading of the feed semantics. If MBP-10 data is
> ever purchased, run the deferred check for 2–3 days each from
> 2017 / 2020 / 2023 / 2025 / Q1-2026 before trusting any result that
> depends critically on absolute book depth.

---

## 1. Files created or changed

### Step 0 — environment + core decision
| File | Purpose |
|---|---|
| `.venv/` | project virtualenv (Python 3.12.11; databento 0.80, databento-dbn 0.61, pytest, maturin, numpy, pandas, pyarrow) |
| `.rustup/`, `.cargo/` | repo-local Rust toolchain 1.96.1 (rustc + cargo, minimal profile) |
| `config/config.toml` | central config: dataset, archive path + measured size (106G), storage tiers, throughput/determinism targets, engine policies, roll/cost placeholders, blocked-item log |

**Decision (Phase 0 Option A): Rust + PyO3/maturin core** (`gc_core`), with the
`dbn` crate v0.61 decoding `.dbn.zst` directly in Rust — one decoder for
historical and live (Critical Rule 3).

### Step 1 — project structure
Plan-mandated tree created: `core/`, `src/{databento_io, mbo_engine, order_book,
queue_engine, calendar_mod, features, aggregation, labeling, datasets, models,
training, evaluation, policy, live/monitoring, utilities}`, `tests/`,
`benchmarks/`, `notebooks/`, `data/{processed,features,labels,calendar,sample_index}`,
`reports/`. Root `pyproject.toml` (src-layout, editable install).
*Deviation:* plan's `src/calendar/` is named `src/calendar_mod/` (a top-level
package named `calendar` would shadow the Python stdlib module).

| File | Purpose |
|---|---|
| `pyproject.toml` | Python packaging (editable, src layout) |
| `src/utilities/config.py` | typed TOML config loader (dataclasses) |
| `src/databento_io/sessions.py` | date -> daily-file resolution, dev-slice listing |

### Step 2 — inventory audit
| File | Purpose |
|---|---|
| `scripts/inventory_audit.py` | full-archive audit per plan Step 2 |
| `reports/archive_audit.md` | audit report: **2,774 files, 112.9 GB, all metadata readable, DBN version 1 uniformly, dataset/schema uniform, only 2020-02-28 missing, 16 degraded days** |
| `reports/archive_audit_files.parquet` | per-file table |
| `reports/quality_mask.csv` | R10 per-date quality mask seed (ok/degraded/missing) |

Raw archive set **read-only** (`chmod a-w`) after the audit passed, per the
v2.5 storage rule. Dev slice selected and verified: **2026-01-04 .. 2026-01-16**
(12 sessions, all status ok, roll-free, front contract GCG6).

### Steps 3–4 — compiled core (Phase 1 engine)
| File | Purpose |
|---|---|
| `core/Cargo.toml`, `core/pyproject.toml` | gc_core crate (pyo3 0.27, dbn 0.61, abi3-py312, LTO release) |
| `core/src/types.rs` | Order/incident types, FNV-1a deterministic hash |
| `core/src/book.rs` | order store + price-level book (BTreeMap levels, per-level FIFO), Globex modify-priority rules, both Milestone-1 views, cross-view consistency check, deterministic digest |
| `core/src/engine.rs` | event engine: A/C/M/T/F/R, ts_event+F_LAST matching-event grouping, T/F reconciliation, snapshot handling, sequence protection, incidents, R1 invariants, per-instrument books |
| `core/src/lib.rs` | PyO3 bindings + Rust-side `.dbn.zst` replay (GIL released) |
| `src/mbo_engine/engine.py` | thin Python wrapper: config-driven, DBN symbology (iid -> contract), front-contract detection, points-scaled views |

### Step 5 — verification
| File | Purpose |
|---|---|
| `tests/test_engine_synthetic.py` | 26 synthetic-exchange tests (known answers by construction) |
| `tests/test_replay_real.py` | 7 real-data tests: zero-error invariants, determinism, independent pure-Python cross-check |
| `benchmarks/throughput_bench.py` | Phase 0 CI benchmark -> `reports/throughput_benchmark.md` |
| `scripts/milestone1_demo.py` | Milestone 1 output -> `reports/milestone1_book.md` |
| `reports/dev_slice_replay.json` | 12-session invariant sweep results |

## 2. Key empirical findings (verified against raw data, not assumed)

1. **Daily files are self-contained**: each opens with per-instrument `R`
   (clear) + snapshot `A` records (`F_SNAPSHOT` flag) rebuilding all resting
   orders. Single-file replay is correct; no cross-day stitching needed.
2. **`F` (Fill) records never mutate the book.** The mutation always arrives
   as an explicit follow-up in the same matching event: `C` after a full fill
   (10,247/10,270 cases on 2026-01-04), `M` with reduced size after a partial
   fill. The engine's first draft assumed F-reduces-order and produced 10,268
   phantom "unknown cancels"; the corrected engine produces **zero**.
3. **A `C` following an `F` at the same ts_event is a fill-removal, not a
   trader cancel.** The engine classifies `cancels_pulled` vs
   `cancels_fill_removal` — essential for every Phase 2 cancel-behavior
   feature.
4. **Fills exceeding displayed size are iceberg executions** (hidden
   quantity), counted as `fills_exceeding_displayed` — input to Phase 2/3
   iceberg features, not an error.
5. **Auction uncross reconciles as F = 2×T** (both resting sides get fill
   attribution; T reports volume once). Continuous trading reconciles F = T.
6. **Pre-open books legitimately cross/lock** (22:00–23:00 UTC indicative
   price formation); crossed-book incidents cluster there and are counted,
   not fatal.

## 3. Tests run and results

| Suite | Result |
|---|---|
| `tests/test_engine_synthetic.py` — 26 tests: book construction both views, cancel/clear, instrument isolation, fill semantics (F+M partial, F+C full), pull-vs-fill-removal classification, iceberg fills, auction F=2T, T-only volume rule, Globex modify priority (decrease keeps / increase loses / price change moves+loses), duplicate/unknown/sequence protections, T/F mismatch incidents, crossed-book detection (incl. transient-in-group non-flagging), digest determinism + sensitivity, halt policy | **26/26 PASS** |
| `tests/test_replay_real.py` — 7 tests on real session 2026-01-04: not halted; 8 error counters exactly zero; cross-view consistency for top-10 instruments; T/F reconciliation ≥99% clean; front-contract identification; replay-twice identical digest; **top-10 bid+ask exactly equal to an independent pure-Python reference book** | **7/7 PASS** |
| `benchmarks/throughput_bench.py` (2026-01-06, 4,379,602 records) | **PASS**: 4.61M / 4.84M ev/s single core (**23× the 200k target**, above the 500k burst target); identical digest + identical full stats across runs |
| 12-session dev-slice sweep (34.3M records total) | **ALL PASS**: zero error counters, views consistent, never halted, front = GCG6 every session |

## 4. Known limitations

1. **MBP-10 cross-check not run (plan Milestone 1 verification, R1 defense a)
   — DEFERRED by user decision 2026-07-09 (see verdict box above).** Requires
   MBP-10 data for verification days — not owned, not being purchased in this
   phase. *Interim mitigation (accepted):* independent pure-Python reference
   book agrees exactly on top-10 both sides. **No longer blocks Phase 2**, but
   Phase 1 remains not-MBP-10-snapshot-verified until the data is purchased.
2. **T/F partial-implied residue**: ~0.01–0.1% of execution groups have
   T>F>0 (outright aggressor partially matched against implied spread
   liquidity). Logged as incidents per the plan's reconciliation rule;
   correctly excluded from error gates. A finer per-group implied-leg
   attribution could reclassify these (Phase 2 candidate).
3. **`unknown_fill` 0–4 per session**: F records whose order id was never
   added (implied-leg attribution edge). Counted, logged, negligible.
4. **Roll policy**: config placeholder written; the full-archive roll ledger
   is Build Order Step 12.5, not Phase 1.
5. **Storage tier deviation**: processed outputs live inside the repo on the
   SATA capacity tier (sandbox restricted to repo). Re-point to NVMe before
   full-archive processing (noted in config).
6. **Sequence tracking** is per Databento channel_id with snapshot exemption;
   CME packet-level gap detection (live reconnect handling) is a Phase 10
   concern.
7. **Live mode** (snapshot init over live feed, reconnection) is Phase 10;
   the engine's snapshot/R handling is the same code path it will use.

## 5. Phase gate

- Order store, book reconstruction, all MBO actions, event boundaries,
  T/F reconciliation, protections, invariants: **DONE, tested**
- Milestone 1 (both views, top-10, spread, size/count per level): **DONE** (`reports/milestone1_book.md`)
- Determinism (replay-twice byte-identical): **PASS** (CI benchmark)
- Throughput (≥200k ev/s sustained): **PASS** at ~4.5M ev/s
- MBP-10 verification: **DEFERRED (user decision 2026-07-09)** — out of
  scope / not yet purchased; interim R1 defense = pure-Python cross-check +
  Phase 1 test suite. Phase 1 is not MBP-10 snapshot-verified.

**Gate resolution:** the phase-progression rule required either the MBP-10
data or an explicit deferral; option (b) — explicit deferral — was chosen by
the user on 2026-07-09. **Phase 2 (order lifecycle and queue engine) is
cleared to start.** The deferred check stays on the backlog alongside the
April-2026+ backfill (see `config/config.toml` blocked-items log).
