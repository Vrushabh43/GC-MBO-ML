# Known gaps in the raw GC MBO archive

Status as of 2026-07-07 (post-backfill). Archive target span: 2017-05-21 .. 2026-03-31.
On disk: 2,774 daily files, all consolidated in `daily/` (one file per day,
no duplicates, ~113 GB compressed). Per-job Databento reports (manifest,
condition, metadata) are preserved in `job_metadata/<job-id>/`.
ARCHIVE COMPLETE except the single unrecoverable day below.

## Range gaps

None. The two ranges that failed in the first download run (2017-12-31 ..
2018-11-30 and 2022-07-31 .. 2023-06-30, `403 auth_account_locked`) were
backfilled on 2026-07-07 with fresh keys (jobs GLBX-20260707-WHQCUMJE3H and
GLBX-20260707-8VGRQV6KKV, 288 daily files each, VERIFIED OK).

## Single-day gaps (upstream — Databento has no data)

| Date       | Weekday | Reason |
|------------|---------|--------|
| 2020-02-28 | Friday  | Databento MBO billable size = 0; condition = `degraded`. Peak COVID-crash session. Unrecoverable from this vendor. |

Per plan R10: quarantined, never patched. The Step 2 inventory audit must
carry this date in the quality mask, and rolling-window state must not span
the hole (treat 2020-02-27 -> 2020-03-01 like a session gap).

## Degraded-but-delivered days (Databento condition != available)

Complete list from scanning all 10 job dirs' `condition.json` (2026-07-07):
2017-11-13, 2018-10-21, 2019-01-15, 2019-02-22, 2019-03-13, 2019-03-26,
2020-02-27, 2020-06-30, 2020-07-01, 2021-12-05, 2022-01-02, 2025-09-17,
2025-09-24, 2025-11-28, 2026-03-15, 2026-03-16.
Files exist on disk and decode; flag in the R10 quality mask at audit time.
