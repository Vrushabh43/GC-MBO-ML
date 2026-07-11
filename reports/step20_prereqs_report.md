# Step 20 Prerequisites Report — Calendar, Pipeline Speed, Purged Splits

Date: 2026-07-11 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Prep for Step 20 (Model A + go/no-go gate)

## Verdict: **ALL THREE PREREQUISITES COMPLETE**

---

## 1. Macro release calendar (plan Phase 4.5) — the HIGH-impact set is complete

- **Ingested 2017–2026, 442 events total** (`data/calendar/events.csv`):
  FOMC statements (79, high), **CPI (121, high)**, **Employment
  Situation/NFP (121, high)**, PPI (121, medium).
- Source: official BLS per-release schedule pages via Wayback Machine
  snapshots (bls.gov blocks direct fetch; the archived pages are the same
  official tables); every row carries its exact snapshot URL. The PPI
  2018 gap (no captures of that page exist — verified via the CDX index)
  was backfilled from the archived BLS **monthly schedule calendars**.
- `scripts/fetch_bls_calendar.py` re-harvests with a **coverage gate**
  (>45-day hole in any series fails the run) and caches raw HTML
  (`data/calendar/.bls_cache/`, gitignored).
- Label EXCLUSION and the live blackout act on HIGH events — that set
  (CPI, NFP, FOMC) is now complete. Still pending (tag-tier only,
  documented in `data/calendar/README.md`): BEA GDP/PCE, Treasury
  auctions, FOMC minutes.

## 2. Pipeline speed-up — profile-driven, in-place, one code path

**Honest correction**: the Phase 4 report guessed compose-at-sample-
instants was "~10×". Profiling showed compose is only ~35% of per-row
cost — but the sample-index loop compounded it, and the measured result
exceeded the guess where it matters:

- **Ingest/compose split** (`FeatureEngine`, `NormalizedFeatureEngine`):
  `step() = ingest() + compose()` remains the live API; batch drivers
  ingest every row and compose (a **pure read**, tested) only at sample
  instants. Percentile/robust-z trackers moved to an **internal 1-second
  clock** so their state is independent of compose cadence — the
  train/serve parity requirement.
- **Sample selection at half-second slots**: clock candidates on second
  boundaries, event-trigger evaluation on half-seconds (still ≤1 extra
  sample/second under the min-spacing rule; trigger instants now
  slot-aligned — documented semantic change, class balance and
  effective-N unchanged).
- **Parallel + resumable driver** (`build_sample_index.py`): one session
  per worker (hardware profile), summary sidecars, `--force` to rebuild.

**Measured**: Sunday-session selection+labels 110 s → **5 s (~22×
serial)**; full 12-session dev-slice index **7 h 40 m → 2 m 23 s
(~190×)**. Statistics preserved (h30 effective-N per weekday session
~2,700, identical class balance). Full suite green after the refactor.

## 3. Purged + embargoed splits (plan Phase 9)

`src/evaluation/splits.py`:

- **Frozen outer split in config** `[splits]` (v2.4, do-not-change):
  train ≤2023 / validate 2024 / test 2025 / holdout Q1-2026 (touched
  once — R7). `frozen_segment(date)` is the single authority.
- **Purging**: every training sample whose `label_end_ts` (Phase 6 anchor,
  max horizon) reaches into the eval period is removed — enforced by an
  assert inside the loader, not just intent.
- **Embargo**: one full session between train and eval (config; plan
  recommendation), plus the 10-minute minimum time embargo whenever the
  session embargo is bypassed.
- **Walk-forward folds only** — the fold generator is the sole split API;
  no single-split or shuffle entry point exists. Fold construction
  asserts chronology.
- **Effective-N audit per fold** (train and eval sides) — the plan
  requires it beside every metric.

## 4. Tests

| Suite | Result |
|---|---|
| Calendar-affected suites re-run after ingestion | **39/39 PASS** |
| Feature/normalization suites after the refactor (incl. one-code-path step≡run, past-only, determinism) | **64/64 PASS** |
| `tests/test_splits.py` — frozen boundaries, fold chronology + embargo, degenerate-request refusal, purge known-answers, effective-N audit, real dev-slice fold with the purge invariant asserted | **8/8 PASS** |
| Full suite | **227/227 PASS** |

## 5. Step 20 readiness

Ready to start Model A. Remaining knowns going in:
1. **Pre-register the gate criteria in config BEFORE training** (plan
   Phase 8 — first action of Step 20, not an afterthought).
2. The gate needs ≥20 out-of-sample trading days ⇒ build the sample index
   for a larger slice (now cheap: ~12 s/session/worker; e.g. 6 months
   ≈ 25 min on 10 workers). Storage note: re-point processed dirs to NVMe
   per config deviation note before very large slices.
3. Model A trains on engineered features at sample instants (the Phase 3/4
   vectors), 30 s horizon, `_norm` variants, uniqueness weights optional
   (test both — plan Phase 6).
