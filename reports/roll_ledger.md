# Step 12.5 — active-contract ledger (full archive)

Generated: 2026-07-10T12:32:01+00:00
Sessions: 2017-05-21 .. 2026-03-31 (2,774) | rule: volume_cross x2 (past-only, effective next session) | ledger: `data/calendar/contract_ledger.parquet`

## Verification

| check | result | detail |
|---|---|---|
| all sessions have an active outright | PASS | 2774 sessions |
| dev slice active == GCG6 (known ground truth) | PASS | ['GCG6'] |
| active never expired | PASS | min days_to_expiry = 25 |
| no forced rolls (volume rule always led) | PASS | forced_rolls = 0 |
| roll targets strictly later expiries | PASS | 45 rolls |
| roll cadence ~6/year (bimonthly cycle) | PASS | 45 rolls over 8.9y = 5.1/y |
| rolls target the liquid GJMQVZ cycle | PASS | 100.0% in cycle |
| active dominates volume (median share > 0.7) | PASS | median share = 0.97 |
| leader == active on >= 93% of sessions | PASS | 96.1% |

## Rolls (45)

| date | new active | days-to-expiry at roll | active share that day |
|---|---|---|---|
| 2017-05-31 | GCQ7 | 90 | 0.97 |
| 2017-07-31 | GCZ7 | 149 | 0.97 |
| 2017-12-01 | GCG8 | 87 | 0.99 |
| 2018-02-01 | GCJ8 | 84 | 0.99 |
| 2018-03-29 | GCM8 | 90 | 0.97 |
| 2018-05-31 | GCQ8 | 90 | 0.99 |
| 2018-07-31 | GCZ8 | 149 | 0.98 |
| 2018-12-02 | GCG9 | 86 | 0.97 |
| 2019-02-01 | GCJ9 | 84 | 0.98 |
| 2019-03-29 | GCM9 | 89 | 0.98 |
| 2019-05-31 | GCQ9 | 89 | 0.98 |
| 2019-08-01 | GCZ9 | 148 | 0.97 |
| 2019-11-29 | GCG0 | 89 | 0.98 |
| 2020-02-02 | GCJ0 | 86 | 0.98 |
| 2020-03-30 | GCM0 | 88 | 0.94 |
| 2020-05-29 | GCQ0 | 90 | 0.97 |
| 2020-08-02 | GCZ0 | 149 | 0.92 |
| 2020-11-29 | GCG1 | 87 | 0.95 |
| 2021-01-31 | GCJ1 | 87 | 0.96 |
| 2021-03-31 | GCM1 | 89 | 0.98 |
| 2021-05-28 | GCQ1 | 91 | 0.98 |
| 2021-08-01 | GCZ1 | 150 | 0.92 |
| 2021-11-29 | GCG2 | 87 | 0.91 |
| 2022-01-30 | GCJ2 | 87 | 0.97 |
| 2022-03-31 | GCM2 | 89 | 0.97 |
| 2022-05-30 | GCQ2 | 91 | 0.97 |
| 2022-07-31 | GCZ2 | 150 | 0.91 |
| 2022-12-01 | GCG3 | 85 | 0.98 |
| 2023-01-31 | GCJ3 | 85 | 0.97 |
| 2023-03-31 | GCM3 | 89 | 0.98 |
| 2023-05-30 | GCQ3 | 91 | 0.94 |
| 2023-07-31 | GCZ3 | 149 | 0.95 |
| 2023-11-30 | GCG4 | 89 | 0.97 |
| 2024-01-31 | GCJ4 | 86 | 0.97 |
| 2024-03-28 | GCM4 | 90 | 0.97 |
| 2024-05-31 | GCQ4 | 89 | 0.98 |
| 2024-07-31 | GCZ4 | 149 | 0.94 |
| 2024-11-29 | GCG5 | 89 | 0.97 |
| 2025-01-31 | GCJ5 | 87 | 0.97 |
| 2025-03-30 | GCM5 | 88 | 0.97 |
| 2025-05-30 | GCQ5 | 89 | 0.97 |
| 2025-07-31 | GCZ5 | 151 | 0.91 |
| 2025-11-27 | GCG6 | 90 | 0.97 |
| 2026-01-30 | GCJ6 | 88 | 0.94 |
| 2026-03-30 | GCM6 | 88 | 0.94 |

Expiry rule: third-to-last WEEKDAY of the delivery month (v1 — CME
holiday calendar not yet ingested; can shift days_to_expiry by a day
or two around holidays, never the roll decision, which is
volume-driven). At a roll, downstream consumers must reset rolling
windows, normalization state, and regime percentiles, and never
stitch features or labels across contracts (plan Phase 1 rule).
