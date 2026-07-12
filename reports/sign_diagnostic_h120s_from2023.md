# Gate-iteration sign-skill diagnostic (bull vs bear, given a move) — h=120s

Generated: 2026-07-12T08:46:15+00:00 | NOT the gate — the GO/NO-GO verdict only comes from run_gate.py
| train 2023-01-01..2023-12-31 directional rows 1,354,245 | OOS 2024-01-02..2024-03-28 directional rows 305,366
| Step 20 reference: multiclass implied sign AUC **0.5558**

| feature set | features | sign AUC (OOS) |
|---|---|---|
| v1 (Step 20 set) | 82 | **0.5045** |
| v1 + signed_v2 | 102 | **0.5041** |
| signed_v2 only | 20 | **0.5204** |

## Top 20 by gain (v1 + signed_v2 sign model)

| rank | feature | family | gain |
|---|---|---|---|
| 1 | f_d_scale | scales_regime | 317,331 |
| 2 | f_sigma_dist_pct | scales_regime | 275,018 |
| 3 | f_d_scale_pct | scales_regime | 271,619 |
| 4 | f_v_scale_pct | scales_regime | 260,086 |
| 5 | f_sigma_600s | scales_regime | 218,599 |
| 6 | f_sigma_dist | scales_regime | 117,053 |
| 7 | f_cancel_before_touch_rate_l | order_survival | 99,810 |
| 8 | f_sigma_30s | scales_regime | 70,912 |
| 9 | f_spread_ticks_pctile | spread | 69,064 |
| 10 | f_v_scale | scales_regime | 62,432 |
| 11 | f_liquidity_survival_ratio_l | order_survival | 57,497 |
| 12 | f_iceberg_score_bid_l | iceberg | 54,709 |
| 13 | f_iceberg_score_ask_l | iceberg | 51,623 |
| 14 | f_order_lifetime_ms_l | order_lifetime | 41,941 |
| 15 | f_order_lifetime_chain_adj_ms_l | order_lifetime | 38,996 |
| 16 | f_failed_sweeps_l | sweeps | 37,123 |
| 17 | f_book_imbalance_outer | signed_v2 | 36,246 |
| 18 | f_hidden_fill_imbalance_l | signed_v2 | 35,324 |
| 19 | f_aggr_delta_l_norm | aggr_delta | 34,247 |
| 20 | f_aggr_delta_l | aggr_delta | 29,905 |

Runtime: 1.3 min
