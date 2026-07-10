# Step 12.5 Completion Report — Economic Calendar, Roll Policy, Contract Ledger

Date: 2026-07-10 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Build Order Step 12.5 (plan Phase 4.5 + Phase 1 roll policy)

## Verdict: **COMPLETE** — with the macro-release calendar explicitly partial (FOMC ingested; CPI/PPI/NFP/PCE/GDP/auctions pending external data)

---

## 1. Active-contract ledger (full archive, emitted AND verified)

- **Stage 1 — volume scan**: `scripts/scan_archive_volumes.py` + a new lean
  Rust scanner (`gc_core.scan_t_volumes`, ~12M records/s, no book work)
  tallied per-instrument records and T volume for **all 2,774 sessions**
  (10 workers, resumable; per-session parquet in `data/processed/roll_scan/`).
- **Stage 2 — ledger**: `scripts/build_roll_ledger.py` applies the config
  `[roll]` volume-cross rule — roll when the candidate's daily volume
  exceeds the active contract's for 2 consecutive sessions, **effective the
  next session** (decision uses only completed sessions; past-only).
  Output: `data/calendar/contract_ledger.parquet`, one row per session:
  active symbol/instrument_id/volume/share, expiry, **days_to_expiry**,
  candidate volume, cross streak, roll flag, session volume leader.
- **Consumer API**: `src/calendar_mod/roll_ledger.py` — `active(date)`,
  `roll_dates()`, `crosses_roll(d0,d1)` (the never-stitch-across-rolls
  guard for every downstream window/feature/label).

**DOCUMENTED DEVIATION (plan conflict, resolved in the plan's favor of
intent):** the plan's literal words say roll when "the *next month's*"
volume crosses. GC's calendar-adjacent month is usually an illiquid dead
month (Jul/Sep/Nov), and the literal rule demonstrably ladders the active
contract through it — 98 "rolls"/8.9y with active-share ≈ 0.00 days
(June→July→August). The implemented candidate is the **volume successor**
(highest-volume outright with a later expiry), which matches the plan's own
front-by-volume definition. The verification gate distinguishes these
empirically (the literal variant FAILS three checks; the implemented rule
passes all).

**Verification (all PASS — `reports/roll_ledger.md`):** 2,774/2,774
sessions have an active outright; dev-slice ground truth GCG6 exact; active
never expired (min 25 days-to-expiry, always rolled by volume — zero forced
rolls); roll targets strictly later expiries; **45 rolls = 5.1/year, 100%
into the liquid G/J/M/Q/V/Z cycle**; median active volume share 0.97;
session volume leader == active on 96.1% of sessions (the gap is roll-eve
days by construction).

## 2. Economic calendar module (plan Phase 4.5)

- **Session phases** (`src/calendar_mod/session_phase.py`): DST-aware
  America/New_York clock (matches the Phase 1 empirical 23:00/22:00 UTC
  open); phases maintenance/asia/london/ny/post + COMEX gold settlement
  window flag (13:29–13:30 ET). All boundaries in config `[calendar]`.
- **Scheduled events** (`src/calendar_mod/events.py`): versioned CSV
  ingestion (`data/calendar/`, schema + rules in its README — release
  *times* only, never realized values); features
  `seconds_to_next/since_last_scheduled_event` + tier encodings; live
  **blackout** (2 min pre / 5 min post HIGH-impact, config); **label
  hygiene** `label_window_policy` (high→exclude, medium→tag, config);
  `coverage_warning` makes missing calendar coverage LOUD so hygiene can
  never silently run blind.
- **Seed data — FOMC 2017–2026**: 79 statement events
  (`data/calendar/events_fomc.csv` → merged `events.csv`), dates fetched
  from federalreserve.gov (provenance in each row + generator
  `scripts/make_fomc_events.py`); instant = 14:00 ET on the final meeting
  day, DST-verified (18:00 UTC summer / 19:00 UTC winter). Deliberately
  excluded, not fabricated: 2019-10-04 unscheduled call, 2020 March
  emergency actions & cancelled meeting, notation votes, FOMC minutes.

## 3. Files created or changed

| File | Change |
|---|---|
| `core/src/lib.rs` | **new** `scan_t_volumes` lean scanner (GIL-released) |
| `scripts/scan_archive_volumes.py` | **new** — resumable 10-worker archive scan |
| `scripts/build_roll_ledger.py` | **new** — ledger build + 9-check verification gate |
| `scripts/make_fomc_events.py` | **new** — FOMC events generator (provenance embedded) |
| `src/calendar_mod/{session_phase,events,roll_ledger}.py` | **new** — the calendar module |
| `config/config.toml` | `[calendar]` section; `[roll]` extended (scan dir, ledger path, expiry rule) |
| `data/calendar/{README.md,events.csv,events_fomc.csv}` | **new** — versioned calendar dataset (git-tracked) |
| `data/calendar/contract_ledger.parquet`, `data/processed/roll_scan/` | derived artifacts (rebuildable, ignored) |
| `tests/test_calendar.py` | **new** — 20 tests |
| `.gitignore` | track calendar CSVs + README |
| `reports/roll_ledger.md` | ledger verification report (45 rolls listed) |

## 4. Tests run and results

| Suite | Result |
|---|---|
| `tests/test_calendar.py` — 20 tests: phase boundaries (10 parametrized), settlement window, **DST awareness** (same 18:30 ET phase from different UTC hours; same UTC hour lands in different phases across DST), event features known-answers, schedule-is-advance-knowledge property, blackout high-only ±window edges, label policy (exclude/tag/ok), loud coverage warning + empty-calendar behavior, ledger access (GCG6 ground truth, roll-boundary crossing guard, dev slice roll-free), symbol/decade resolution + expiry rule incl. December year-wrap | **20/20 PASS** |
| Full suite (Phases 1+2+3 + Step 12.5) | **140/140 PASS** |
| Ledger verification gate (script exit status) | **9/9 checks PASS** |

## 5. Known limitations

1. **Macro calendar coverage is FOMC-only.** CPI, PPI, NFP, PCE, GDP, and
   Treasury-auction schedules are pending external ingestion (BLS/BEA/
   Treasury archives). The module schema, merge flow, blackout, and label
   hygiene are fully functional; `coverage_warning` flags uncovered
   periods loudly. **This must be closed before Phase 7 labeling** (the
   plan's label-hygiene rules need CPI/NFP at minimum).
2. **Expiry rule is weekday-approximate** (third-to-last weekday of the
   delivery month; CME holiday calendar not ingested). Can shift
   `days_to_expiry` by ±1–2 days around holidays; cannot affect the roll
   decision (volume-driven).
3. 2020 March emergency Fed actions are not in the calendar (irregular
   announcement instants; deliberately not fabricated). That period is the
   tagged COVID stratum regardless.
4. Roll-eve sessions correctly retain the OLD active while the market has
   begun migrating (past-only rule) — consumers see this as the ~4% of
   sessions where leader ≠ active.

## 6. Step gate

- Full-archive active-contract ledger emitted: **DONE** (2,774 sessions)
- Ledger verified: **PASS** (9/9 checks; ground truth + cadence + cycle)
- Roll policy semantics (reset windows, never stitch): **API provided**
  (`crosses_roll`), enforcement lands in each downstream consumer
- Calendar module + session phases + blackout + label hygiene: **DONE, tested**
- Versioned calendar dataset with provenance: **DONE** (FOMC seed; rest
  pending external data — tracked as an explicit open item)

**Next: Phase 4/4A — normalization architecture** (sigma_h / v_scale /
d_scale, the `_norm` twins, rolling-window resets at roll boundaries via
this step's ledger).
