# Sample index — effective-sample-size report (Step 19)

Generated: 2026-07-11T22:50:49+00:00
Sessions: 2026-01-04 .. 2026-01-16 (12) | weights: López-de-Prado average uniqueness, per horizon

| session | picked | clock | event-trig | h30 train | **h30 eff-N** | h30 NT/BU/BE | h120 eff-N | h600 eff-N |
|---|---|---|---|---|---|---|---|---|
| 2026-01-04 | 3,410 | 2,889 | 521 | 3,085 | **115** | 0.82/0.10/0.07 | 29 | 5 |
| 2026-01-05 | 65,732 | 64,221 | 1,511 | 64,289 | **2,718** | 0.88/0.06/0.06 | 700 | 143 |
| 2026-01-06 | 62,677 | 61,313 | 1,364 | 61,311 | **2,713** | 0.90/0.05/0.05 | 688 | 145 |
| 2026-01-07 | 66,036 | 64,505 | 1,531 | 64,434 | **2,712** | 0.88/0.05/0.07 | 685 | 149 |
| 2026-01-08 | 64,700 | 63,293 | 1,407 | 63,075 | **2,711** | 0.89/0.06/0.05 | 686 | 155 |
| 2026-01-09 | 59,644 | 58,176 | 1,468 | 57,473 | **2,579** | 0.89/0.06/0.05 | 663 | 144 |
| 2026-01-11 | 3,163 | 2,827 | 336 | 2,884 | **115** | 0.88/0.07/0.04 | 29 | 5 |
| 2026-01-12 | 66,419 | 64,931 | 1,488 | 64,738 | **2,727** | 0.87/0.06/0.07 | 718 | 143 |
| 2026-01-13 | 66,323 | 64,783 | 1,540 | 64,128 | **2,704** | 0.88/0.06/0.06 | 714 | 150 |
| 2026-01-14 | 70,596 | 69,169 | 1,427 | 68,637 | **2,714** | 0.87/0.06/0.07 | 695 | 149 |
| 2026-01-15 | 69,559 | 68,107 | 1,452 | 67,587 | **2,722** | 0.89/0.05/0.06 | 695 | 143 |
| 2026-01-16 | 65,230 | 63,679 | 1,551 | 63,444 | **2,598** | 0.88/0.06/0.06 | 670 | 141 |

## Totals — row count is NOT information count (plan Phase 6)

| horizon | trainable rows | effective N | effective share |
|---|---|---|---|
| 30s | 645,085 | 27,128 | 4.2% |
| 120s | 652,120 | 6,971 | 1.1% |
| 600s | 649,466 | 1,470 | 0.2% |

Label convention (stored with every file): `{'convention': 'touch_opposite', 'cost_round_trip_pts': 0.2, 'direction_ratio': 2.0, 'horizons_s': [30, 120, 600], 'path_resolution_s': 1, 'units': 'points + sigma_h-normalized twins (v2.1 dual-unit)'}`

Uniqueness weights are per-horizon columns in each parquet; Phase 8
training uses them (optionally) and Phase 9 purging uses
`label_end_ts`. Event-trigger caps and the minimum-spacing rule are
config `[samples]`.
