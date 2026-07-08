# Milestone 1 — full book reconstruction (2026-01-06, end of file)

Session file: `glbx-mdp3-20260106.mbo.dbn.zst` | 4,379,602 MBO records in 0.92s (4.78M ev/s) | digest `0xec8ae70fac7ef444` | halted: None

Front contract (by traded volume): **GCG6** (instrument_id 42001025)
Resting orders: 2,316 | cross-view consistency: **OK**

**Best bid** 4504.5 (2 lots, 2 orders)   **Best ask** 4504.8 (3 lots, 1 orders)   **Spread** 0.3 pts

## Top-10 BID levels

| # | price | size (level view) | orders | size (order view) | orders | match |
|---|-------|------|--------|------|--------|-------|
| 1 | 4504.5 | 2 | 2 | 2 | 2 | OK |
| 2 | 4504.4 | 4 | 4 | 4 | 4 | OK |
| 3 | 4504.3 | 4 | 4 | 4 | 4 | OK |
| 4 | 4504.2 | 3 | 3 | 3 | 3 | OK |
| 5 | 4504.1 | 5 | 5 | 5 | 5 | OK |
| 6 | 4504.0 | 9 | 8 | 9 | 8 | OK |
| 7 | 4503.9 | 6 | 6 | 6 | 6 | OK |
| 8 | 4503.8 | 7 | 6 | 7 | 6 | OK |
| 9 | 4503.7 | 5 | 5 | 5 | 5 | OK |
| 10 | 4503.6 | 9 | 9 | 9 | 9 | OK |

## Top-10 ASK levels

| # | price | size (level view) | orders | size (order view) | orders | match |
|---|-------|------|--------|------|--------|-------|
| 1 | 4504.8 | 3 | 1 | 3 | 1 | OK |
| 2 | 4505.0 | 4 | 4 | 4 | 4 | OK |
| 3 | 4505.1 | 3 | 3 | 3 | 3 | OK |
| 4 | 4505.2 | 7 | 6 | 7 | 6 | OK |
| 5 | 4505.3 | 7 | 7 | 7 | 7 | OK |
| 6 | 4505.4 | 6 | 6 | 6 | 6 | OK |
| 7 | 4505.5 | 8 | 7 | 8 | 7 | OK |
| 8 | 4505.6 | 9 | 9 | 9 | 9 | OK |
| 9 | 4505.7 | 9 | 8 | 9 | 8 | OK |
| 10 | 4505.8 | 8 | 8 | 8 | 8 | OK |

## Instruments in session (top 8 by traded volume)

| symbol | instrument_id | records | T volume |
|--------|---------------|---------|----------|
| GCG6 | 42001025 | 3,532,228 | 151,688 |
| GCG6-GCJ6 | 42031409 | 42,668 | 15,449 |
| GCJ6 | 42000890 | 181,931 | 3,898 |
| GCG6-GCM6 | 42039347 | 27,704 | 1,558 |
| GCG6-GCH6 | 42066744 | 5,778 | 1,464 |
| GCH6 | 42020164 | 9,050 | 1,388 |
| GCJ6-GCM6 | 42037374 | 22,840 | 1,276 |
| GCM6 | 19181 | 28,913 | 616 |

## Engine counters

```
adds                         1,613,343
cancel_size_mismatch         0
cancels                      1,607,023
cancels_fill_removal         135,260
cancels_pulled               1,471,763
clears                       259
crossed_book                 0
duplicate_add                0
event_groups                 4,064,625
f_volume                     178,788
fill_overrun                 0
fills                        148,833
fills_exceeding_displayed    571
groups_f_without_t           0
groups_t_without_f           2,826
groups_tf_matched            98,665
groups_tf_matched_auction    1,484
groups_tf_mismatch           560
modifies                     894,488
others                       0
records                      4,379,602
sequence_regression          0
snapshot_records             5,550
store_levels_mismatch        0
t_volume                     180,970
t_volume_buy                 83,368
t_volume_sell                87,051
tf_reconcile_mismatch        560
trades                       115,656
unknown_cancel               0
unknown_fill                 0
unknown_modify               0
```
