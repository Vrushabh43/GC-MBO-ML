# Sample index — effective-sample-size report (Step 19)

Generated: 2026-07-10T14:04:58+00:00
Sessions: 2026-01-04 .. 2026-01-16 (12) | weights: López-de-Prado average uniqueness, per horizon

| session | picked | clock | event-trig | h30 train | **h30 eff-N** | h30 NT/BU/BE | h120 eff-N | h600 eff-N |
|---|---|---|---|---|---|---|---|---|
| 2026-01-04 | 3,850 | 3,102 | 748 | 3,407 | **115** | 0.82/0.10/0.07 | 29 | 5 |
| 2026-01-05 | 71,676 | 70,095 | 1,581 | 69,917 | **2,718** | 0.88/0.06/0.06 | 700 | 143 |
| 2026-01-06 | 69,458 | 67,892 | 1,566 | 67,599 | **2,713** | 0.91/0.04/0.05 | 688 | 145 |
| 2026-01-07 | 71,857 | 70,277 | 1,580 | 69,723 | **2,712** | 0.89/0.05/0.06 | 686 | 150 |
| 2026-01-08 | 71,154 | 69,602 | 1,552 | 68,892 | **2,711** | 0.90/0.05/0.05 | 686 | 157 |
| 2026-01-09 | 66,426 | 64,873 | 1,553 | 64,220 | **2,599** | 0.90/0.05/0.05 | 667 | 144 |
| 2026-01-11 | 3,705 | 3,204 | 501 | 3,227 | **115** | 0.88/0.07/0.04 | 29 | 5 |
| 2026-01-12 | 72,237 | 70,664 | 1,573 | 70,177 | **2,729** | 0.87/0.06/0.07 | 718 | 143 |
| 2026-01-13 | 72,023 | 70,451 | 1,572 | 69,804 | **2,726** | 0.88/0.06/0.06 | 718 | 150 |
| 2026-01-14 | 75,695 | 74,152 | 1,543 | 73,123 | **2,715** | 0.87/0.06/0.06 | 695 | 149 |
| 2026-01-15 | 74,780 | 73,217 | 1,563 | 72,210 | **2,726** | 0.89/0.05/0.06 | 695 | 143 |
| 2026-01-16 | 70,581 | 69,015 | 1,566 | 68,154 | **2,598** | 0.88/0.06/0.06 | 671 | 141 |

## Totals — row count is NOT information count (plan Phase 6)

| horizon | trainable rows | effective N | effective share |
|---|---|---|---|
| 30s | 700,453 | 27,176 | 3.9% |
| 120s | 708,963 | 6,982 | 1.0% |
| 600s | 706,039 | 1,475 | 0.2% |

Label convention (stored with every file): `{'convention': 'touch_opposite', 'cost_round_trip_pts': 0.2, 'direction_ratio': 2.0, 'horizons_s': [30, 120, 600], 'path_resolution_s': 1, 'units': 'points + sigma_h-normalized twins (v2.1 dual-unit)'}`

Uniqueness weights are per-horizon columns in each parquet; Phase 8
training uses them (optionally) and Phase 9 purging uses
`label_end_ts`. Event-trigger caps and the minimum-spacing rule are
config `[samples]`.
