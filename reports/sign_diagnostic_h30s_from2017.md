# Gate-iteration sign-skill diagnostic (bull vs bear, given a move) — h=30s

Generated: 2026-07-12T08:44:55+00:00 | NOT the gate — the GO/NO-GO verdict only comes from run_gate.py
| train 2017-05-21..2023-12-31 directional rows 1,592,868 | OOS 2024-01-02..2024-03-28 directional rows 56,176
| Step 20 reference: multiclass implied sign AUC **0.5558**

| feature set | features | sign AUC (OOS) |
|---|---|---|
| v1 (Step 20 set) | 82 | **0.5797** |
| v1 + signed_v2 | 102 | **0.5785** |
| signed_v2 only | 20 | **0.5679** |

## Top 20 by gain (v1 + signed_v2 sign model)

| rank | feature | family | gain |
|---|---|---|---|
| 1 | f_d_scale | scales_regime | 126,979 |
| 2 | f_replenish_tilt_m | signed_v2 | 96,555 |
| 3 | f_aggr_delta_ratio_l | aggr_delta | 79,020 |
| 4 | f_sigma_600s | scales_regime | 76,298 |
| 5 | f_aggr_delta_l_norm | aggr_delta | 75,811 |
| 6 | f_d_scale_pct | scales_regime | 75,765 |
| 7 | f_v_scale_pct | scales_regime | 71,142 |
| 8 | f_sigma_dist_pct | scales_regime | 66,472 |
| 9 | f_cancel_before_touch_rate_l | order_survival | 56,653 |
| 10 | f_aggr_delta_l | aggr_delta | 50,221 |
| 11 | f_sigma_dist | scales_regime | 47,904 |
| 12 | f_failed_sweeps_l | sweeps | 45,663 |
| 13 | f_liquidity_survival_ratio_l | order_survival | 44,912 |
| 14 | f_book_imbalance_total | book_imbalance | 42,969 |
| 15 | f_iceberg_score_bid_l | iceberg | 34,554 |
| 16 | f_order_lifetime_ms_l | order_lifetime | 31,676 |
| 17 | f_liquidity_age_ask_s | liquidity_age | 31,177 |
| 18 | f_iceberg_score_ask_l | iceberg | 31,109 |
| 19 | f_price_progress_ticks_m | price_progress | 30,996 |
| 20 | f_sigma_30s | scales_regime | 30,882 |

Runtime: 5.2 min
