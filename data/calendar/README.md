# Versioned calendar data (plan Phase 4.5 / Step 12.5)

This directory is the **versioned calendar dataset** that ships with the
processed data. Two kinds of content live here:

## 1. Scheduled-release events — `events.csv` (+ per-source files)

**Schema** (UTF-8 CSV, header required):

| column | format | meaning |
|---|---|---|
| `ts_utc` | ISO-8601, e.g. `2024-03-12T12:30:00+00:00` | scheduled release instant (UTC) |
| `name`   | free text, e.g. `CPI`, `NFP`, `FOMC statement` | event name |
| `tier`   | `high` \| `medium` \| `low` | impact tier (drives blackout + label hygiene) |
| `source` | free text/URL | provenance of THIS row |

Rules:
- **Never edit rows in place** — add a new file (e.g. `events_fomc_v2.csv`)
  and re-merge; keep provenance in `source`.
- Release **times only**, never realized values (leakage rule).
- The loader (`calendar_mod.events.EventCalendar`) accepts any file with
  this schema via `ingest_csv`; `events.csv` is the default merged file
  named in `config [calendar] events_file`.

**Status (2026-07-11):**

| series | tier | coverage 2017–2026 | source |
|---|---|---|---|
| FOMC statements | high | 79 events, complete | federalreserve.gov (`events_fomc.csv`) |
| CPI | high | 121 events, complete, gap-gated | BLS schedule pages via Wayback (`events_bls.csv`) |
| Employment Situation (NFP) | high | 121 events, complete, gap-gated | BLS via Wayback |
| PPI | medium | 121 events, complete (2018 backfilled from BLS monthly calendars) | BLS via Wayback |
| GDP, PCE (BEA) | medium | **pending** | — |
| Treasury auctions, FOMC minutes | medium/low | **pending** | — |

The HIGH-impact set (drives label EXCLUSION and the live blackout) is
complete. Pending series are tag-tier only. `fetch_bls_calendar.py`
re-harvests with a coverage gate (>45-day holes fail the run); raw HTML
snapshots are cached in `.bls_cache/` (gitignored). Do not fabricate
dates; the loader's `coverage_warning` stays the loud guard.

## 2. Contract ledger — `contract_ledger.parquet`

Full-archive active-contract ledger emitted by
`scripts/build_roll_ledger.py` (Step 12.5): one row per session with the
volume-leading outright, the configured volume-cross roll rule's active
contract, roll flags, and days-to-expiry. Verification report:
`reports/roll_ledger.md`.
