# Phase 2 — order lifecycle and queue engine (dev-slice validation)

Generated: 2026-07-09T21:16:40+00:00
Sessions: 2026-01-04 .. 2026-01-16 (12) | outputs: `data/processed/lifecycle`

## Dev-slice sweep — conservation invariants + session diagnostics

Diagnostics are session-level aggregates of the FRONT contract (neutral naming per plan Phase 2; rolling feature versions are Phase 3). `conserve` = every add terminated exactly once AND fill volume reconciled exactly.

| session | records | ev/s | orders | conserve | fill_rate | cancel_before_touch | survival@1t | short_lived_large | refill links | chains |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-01-04 | 283,338 | 3.1M | 105,792 | OK | 0.112 | 0.309 | 0.909 | 0.1170 | 879 | 826 |
| 2026-01-05 | 5,241,755 | 3.0M | 1,940,010 | OK | 0.094 | 0.334 | 0.897 | 0.1325 | 12,899 | 11,903 |
| 2026-01-06 | 4,379,602 | 3.1M | 1,613,343 | OK | 0.094 | 0.320 | 0.902 | 0.1284 | 11,669 | 10,898 |
| 2026-01-07 | 5,471,809 | 3.1M | 1,944,652 | OK | 0.087 | 0.343 | 0.896 | 0.1396 | 12,325 | 11,503 |
| 2026-01-08 | 5,148,863 | 3.2M | 1,781,741 | OK | 0.082 | 0.330 | 0.896 | 0.1325 | 11,199 | 10,325 |
| 2026-01-09 | 4,944,759 | 3.1M | 1,794,847 | OK | 0.086 | 0.312 | 0.897 | 0.1334 | 12,570 | 11,555 |
| 2026-01-11 | 204,048 | 3.2M | 72,231 | OK | 0.138 | 0.306 | 0.898 | 0.0894 | 682 | 611 |
| 2026-01-12 | 6,295,302 | 3.0M | 2,333,656 | OK | 0.098 | 0.336 | 0.889 | 0.1481 | 18,679 | 17,251 |
| 2026-01-13 | 5,857,232 | 3.0M | 2,227,491 | OK | 0.086 | 0.357 | 0.890 | 0.1672 | 15,585 | 14,420 |
| 2026-01-14 | 6,324,097 | 3.1M | 2,326,647 | OK | 0.093 | 0.326 | 0.884 | 0.1472 | 19,191 | 17,737 |
| 2026-01-15 | 5,853,364 | 3.1M | 2,143,639 | OK | 0.084 | 0.347 | 0.883 | 0.1577 | 14,192 | 13,106 |
| 2026-01-16 | 6,281,667 | 2.8M | 2,234,232 | OK | 0.092 | 0.358 | 0.883 | 0.1572 | 15,910 | 14,830 |

## Deep dive — 2026-01-06, front contract GCG6 (instrument_id 42001025)

Replay: 4,379,602 records at 3.11M ev/s | lifecycle digest `0x7065899d9dbd9041`

### Terminal states (front contract)

| state | orders | share |
|---|---|---|
| cancelled | 1,237,643 | 90.435% |
| filled | 128,202 | 9.368% |
| end_of_data | 2,316 | 0.169% |
| partial_cancelled | 383 | 0.028% |

### Lifetimes (non-snapshot orders, ms)

| population | n | p25 | median | p75 | p95 |
|---|---|---|---|---|---|
| filled | 127,960 | 82.6 | 879.5 | 6,491.4 | 304,618.7 |
| pulled | 1,237,144 | 11.9 | 443.1 | 3,845.5 | 141,062.8 |
| pulled, never touched | 396,259 | 160.9 | 1,556.6 | 16,451.9 | 385,305.9 |
| refill links (iceberg clips) | 11,340 | 0.6 | 4.3 | 551.4 | 12,853.3 |

### Queue mechanics (non-snapshot orders)

- queue position at add: median 2, p95 8 orders ahead (volume ahead median 2 lots)
- filled orders terminating at the queue front: 99.2% (FIFO matching, as required)
- distance from same-side best at add: median 0.0 ticks, p95 41.0
- orders the market touched while resting: 70.9%; pulled before ever touched: 29.0%

### Iceberg synthetic-parent chains (heuristic, Critical Rule 8)

- chains: 10,685 on GCG6 (10,898 all instruments); refills per chain: median 1, max 22
- executed-to-displayed ratio: median 0.50, p95 1.00
- link confidence (min per chain): median 0.77

| chain_id | side | members | displayed | filled | exec/disp | duration (s) | min conf |
|---|---|---|---|---|---|---|---|
| 7812133679950 | A | 2 | 75 | 74 | 0.99 | 11.1 | 0.38 |
| 7812144568705 | B | 2 | 2 | 60 | 30.00 | 27.6 | 0.94 |
| 7812135356484 | B | 2 | 2 | 53 | 26.50 | 167.8 | 0.73 |
| 7812146003392 | A | 2 | 3 | 53 | 17.67 | 266.3 | 0.70 |
| 7812148831176 | B | 8 | 50 | 49 | 0.98 | 74.4 | 0.52 |

### Session diagnostics (front contract, neutral naming)

```
orders_terminated                      1368544.0000
orders_entered_live                    1366899.0000
fill_rate                              0.0936
cancel_before_touch_rate               0.3203
liquidity_survival_ratio               0.9022
short_lived_large_order_behavior       0.1284
median_lifetime_ms_raw                 476.8757
median_lifetime_ms_chain_adjusted      484.8348
refill_link_share_of_adds              0.0083
```

**Sweep verdict: ALL SESSIONS CONSERVE**
