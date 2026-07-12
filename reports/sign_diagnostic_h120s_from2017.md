# Gate-iteration sign-skill diagnostic (bull vs bear, given a move) — h=120s

Generated: 2026-07-12T08:51:00+00:00 | NOT the gate — the GO/NO-GO verdict only comes from run_gate.py
| train 2017-05-21..2023-12-31 directional rows 7,919,199 | OOS 2024-01-02..2024-03-28 directional rows 305,366
| Step 20 reference: multiclass implied sign AUC **0.5558**

| feature set | features | sign AUC (OOS) |
|---|---|---|
| v1 (Step 20 set) | 82 | **0.5255** |
| v1 + signed_v2 | 102 | **0.5283** |
| signed_v2 only | 20 | **0.5315** |

## Top 20 by gain (v1 + signed_v2 sign model)

| rank | feature | family | gain |
|---|---|---|---|
| 1 | f_d_scale | scales_regime | 427,947 |
| 2 | f_sigma_600s | scales_regime | 330,375 |
| 3 | f_v_scale_pct | scales_regime | 320,394 |
| 4 | f_d_scale_pct | scales_regime | 316,814 |
| 5 | f_sigma_dist_pct | scales_regime | 306,833 |
| 6 | f_sigma_dist | scales_regime | 206,250 |
| 7 | f_sigma_30s | scales_regime | 131,939 |
| 8 | f_cancel_before_touch_rate_l | order_survival | 124,390 |
| 9 | f_v_scale | scales_regime | 105,819 |
| 10 | f_liquidity_survival_ratio_l | order_survival | 99,534 |
| 11 | f_aggr_delta_l_norm | aggr_delta | 87,731 |
| 12 | f_spread_ticks_pctile | spread | 82,530 |
| 13 | f_aggr_delta_ratio_l | aggr_delta | 76,770 |
| 14 | f_book_imbalance_total | book_imbalance | 75,816 |
| 15 | f_iceberg_score_bid_l | iceberg | 71,745 |
| 16 | f_failed_sweeps_l | sweeps | 68,286 |
| 17 | f_aggr_delta_l | aggr_delta | 61,991 |
| 18 | f_iceberg_score_ask_l | iceberg | 61,879 |
| 19 | f_microprice_disp_ticks_norm | microprice | 60,000 |
| 20 | f_liquidity_age_ask_s | liquidity_age | 58,640 |

Runtime: 4.8 min
