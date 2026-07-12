# GC Futures Order-Flow AI System

Machine-learning trading research system for **COMEX Gold futures (GC)**, built
on full-depth market-by-order (MBO) data. The system reconstructs the limit
order book from raw exchange events, derives order-flow features, and trains a
multi-task quantile model to forecast short-horizon price behaviour — with one
identical code path for historical research and live operation.

- **Data**: Databento, dataset `GLBX.MDP3`, schema `mbo`, symbol `GC.FUT` (parent)
- **Specification**: [`gc_orderflow_plan_v2.md`](gc_orderflow_plan_v2.md) (v2.5) — the
  authoritative working plan. Read it before changing anything.
- **Language**: Python + a compiled performance core (Rust/PyO3 or Cython) for the
  book engine.

---

## Current status (2026-07-07)

| Milestone | Status |
|---|---|
| Raw MBO archive 2017-05-21 → 2026-03-31 | ✅ Complete — 2,774 daily files, ~113 GB compressed |
| Archive consolidated into one folder (`data/raw_mbo/daily/`) | ✅ Done |
| Known gaps documented | ✅ [`data/raw_mbo/KNOWN_GAPS.md`](data/raw_mbo/KNOWN_GAPS.md) |
| Step 0: environment + compiled core decision (Rust/PyO3) | ✅ Done (repo-local toolchain, `.venv`) |
| Step 1: project structure + `config/config.toml` | ✅ Done |
| Step 2: inventory audit, quality mask, dev slice, archive read-only | ✅ [`reports/archive_audit.md`](reports/archive_audit.md) |
| **Phase 1: MBO engine** (order store, book, T/F rule, invariants) | ✅ Built + 33 tests passing, ~4.5M ev/s, deterministic — [`reports/phase1_completion_report.md`](reports/phase1_completion_report.md) |
| Phase 1 MBP-10 cross-check | ⚠️ **Deferred** (user decision 2026-07-09): out of scope / not yet purchased. Interim R1 defense = independent pure-Python cross-check. Phase 1 is **not MBP-10 snapshot-verified**. |
| April 2026+ backfill | ⏳ Not yet purchased |
| **Phase 2: order lifecycle + queue engine** (lifecycle records, FIFO queue positions, iceberg chains) | ✅ Complete — ~2.9M ev/s with tracking, deterministic — [`reports/phase2_completion_report.md`](reports/phase2_completion_report.md) |
| **Phase 3: core order-flow features** (19 features: delta→absorption→MLOFI/sweeps/iceberg/…, unit+property+visual each) | ✅ Complete — 120 tests total, ~1.2M ev/s full recording — [`reports/phase3_completion_report.md`](reports/phase3_completion_report.md) |
| **Step 12.5: economic calendar + roll ledger** (45 rolls verified over 2,774 sessions; FOMC events seeded) | ✅ Complete — 140 tests total — [`reports/step12_5_completion_report.md`](reports/step12_5_completion_report.md) |
| Macro release calendar (CPI/NFP/PPI/PCE/GDP/auctions) | ⚠️ Pending external data — needed before Phase 7 labels ([`data/calendar/README.md`](data/calendar/README.md)) |
| **Phase 4/4A: normalization architecture** (sigma_h/v_scale/d_scale, `_norm` twins, percentiles, era-invariance verified) | ✅ Complete — 165 tests total — [`reports/phase4_completion_report.md`](reports/phase4_completion_report.md) |
| **Phase 5: model input architecture** (event stream 1024×22, flow bars 256×30, tactical 300×23, slow 180×15, regime 71 + standardization layer) | ✅ Complete — 191 tests total — [`reports/phase5_completion_report.md`](reports/phase5_completion_report.md) |
| **Phase 6: labels + sample index** (tradeable-price dual-unit labels; ~700k samples, effective-N 27k @30s) | ✅ Complete — 218 tests total — [`reports/phase6_completion_report.md`](reports/phase6_completion_report.md) |
| Step 20 prerequisites (macro calendar CPI/NFP/PPI ingested; pipeline ~190× faster; purged splits) | ✅ Complete — 227 tests total — [`reports/step20_prereqs_report.md`](reports/step20_prereqs_report.md) |
| **Step 20: Model A + GO/NO-GO gate** (pre-registered; 6 combos on 85M samples 2017–2024) | ⚖️ **NO-GO** — move-timing AUC 0.88, sign AUC 0.556, expectancy < cost — [`reports/step20_completion_report.md`](reports/step20_completion_report.md) |
| **Gate iteration 2: signed_v2 features** (20 sign-carrying features; sign-skill harness; frozen gate re-run) | ⚖️ **NO-GO again** — sign AUC 0.556→0.559, ceiling ≈0.58 (full history); feature lever exhausted — [`reports/gate_iteration2_report.md`](reports/gate_iteration2_report.md) |
| Next iteration levers: sampling/labels per plan — or new *information* (cross-market, macro tag tiers), a scope decision | ⏳ Awaiting direction — Models B/C stay off until the gate passes |

## Repository layout

```
GC/
├── README.md                     ← you are here
├── gc_orderflow_plan_v2.md       ← full project specification (v2.5)
├── data/
│   └── raw_mbo/
│       ├── daily/                ← THE archive: one .dbn.zst file per day
│       │   └── glbx-mdp3-YYYYMMDD.mbo.dbn.zst   (2,774 files)
│       ├── job_metadata/         ← Databento batch-job reports (manifest,
│       │   └── GLBX-*/              condition, metadata) — provenance record
│       ├── KNOWN_GAPS.md         ← authoritative gap & degraded-day list
│       └── download_archive.log  ← full download history
└── scripts/
    └── download_archive.py       ← sequential batch downloader (restartable)
```

## The data

**2,774 daily files spanning 2017-05-21 → 2026-03-31** (every CME Globex
session; Saturdays have no session). Format is exactly as delivered by
Databento: **DBN + zstd** (`.dbn.zst`) — lossless int64 fixed-point prices,
nanosecond timestamps, order IDs, sequence numbers, and flags preserved. Files
are never decompressed to disk; read them directly:

```python
import databento as db
store = db.DBNStore.from_file("data/raw_mbo/daily/glbx-mdp3-20240102.mbo.dbn.zst")
for record in store:
    ...
```

### Known holes — read before processing

- **2020-02-28 is missing and unrecoverable** (Databento has zero MBO data for
  this session; peak COVID crash). Per plan rule R10 it is quarantined, never
  patched: keep it in the quality mask and never let rolling-window state span
  2020-02-27 → 2020-03-01.
- **16 degraded-but-delivered days** (Databento `condition != available`) exist
  and decode fine but must be flagged in the quality mask. Full list in
  [`KNOWN_GAPS.md`](data/raw_mbo/KNOWN_GAPS.md).

### Frozen chronological split (do not change)

| Split | Period |
|---|---|
| Train | 2017 – 2023 (COVID stratum tagged) |
| Validate | 2024 |
| Test | 2025 |
| Final holdout | Q1 2026 — touch once, at the very end |

## Downloading / backfilling data

The archive was downloaded with `scripts/download_archive.py` via Databento
batch jobs, one API key at a time. It reads key files from a `keys/` folder
(`key-N.txt`: line 1 = API key, line 2 = date range like
`2022-07-31 to 2023-06-30`, order-insensitive), submits the job, polls until
packaged, downloads all daily files, then **verifies** (manifest sizes,
calendar completeness, decode test) and writes a done-marker so re-runs skip
finished keys.

The `keys/` folder was deleted after the archive completed — API keys are
provided ad hoc when needed and are never stored in or committed to this repo.
For a future backfill, recreate `keys/key-N.txt` temporarily, run the script,
then delete the folder again.

> ⚠️ The script predates the consolidated layout: it downloads into a
> `data/raw_mbo/<JOB-ID>/` directory. After a new download completes, move the
> daily files into `daily/` and the JSON reports into `job_metadata/<JOB-ID>/`
> (or update the script first). Update `KNOWN_GAPS.md` after any backfill.

Archive cost: $423.92 across the 8 jobs in `download_archive.log`, plus two
earlier jobs (2024-05-31 → 2026-03-31 ranges) downloaded before that log began —
roughly $550 total for the full archive.

## Hardware profile (target machine)

Single Linux workstation: AMD Ryzen 9 3900X (12c/24t), 64 GB RAM, RTX 2080
SUPER 8 GB, NVMe fast tier + `/home` SATA capacity tier. Consequences:

- Raw archive lives on `/home` (capacity tier), immutable after audit.
- Processed Parquet, labels, and model artifacts go to NVMe. Nothing hot on CIFS.
- Historical batch processing: ~10 worker processes, one session per worker.
- Model training: mixed precision, gradient accumulation; 8 GB VRAM is the
  model-capacity ceiling (which enforces the R5 effective-sample-size discipline).

## Build order (from the plan — abbreviated)

1. **Step 0–2**: performance-architecture decision (compiled book engine),
   inventory audit of the raw archive, quality mask, contract-roll ledger.
2. **Phases 1–3**: MBO data engine → order-lifecycle/queue engine → core
   order-flow features. Throughput gates: ≥200k events/s sustained replay,
   ≥500k events/s burst, byte-identical replay determinism (CI test).
3. **Phases 4–6**: normalization architecture (σ_h / v_scale / d_scale),
   economic calendar, model inputs, training samples.
4. **Go/no-go gate**, then **Phases 7–9**: labels (volatility-normalized,
   tradeable-price), multi-task quantile model, purged validation.
5. **Phases 10–11**: live system, execution and policy layer, monitoring.

## Non-negotiable rules (highlights)

- **One code path** for historical and live processing — no vectorized-historical
  vs. streaming-live split, ever (Critical Rule 3 / risk R3).
- **Determinism**: same input file ⇒ byte-identical output, twice.
- **Raw data is immutable**: `.dbn.zst` files are kept exactly as delivered.
- **Never patch data holes** — quarantine and mask them (R10).
- **The Q1 2026 holdout is sacred** — evaluated once, at the end (R7).
- Risks R1–R3 (book-reconstruction bugs, future leakage, train/serve skew)
  outrank all other work. See Appendix A of the plan.
