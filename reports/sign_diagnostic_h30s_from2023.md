# Gate-iteration sign-skill diagnostic (bull vs bear, given a move) — h=30s

Generated: 2026-07-12T08:39:39+00:00 | NOT the gate — the GO/NO-GO verdict only comes from run_gate.py
| train 2023-01-01..2023-12-31 directional rows 244,855 | OOS 2024-01-02..2024-03-28 directional rows 56,176
| Step 20 reference: multiclass implied sign AUC **0.5558**

| feature set | features | sign AUC (OOS) |
|---|---|---|
| v1 (Step 20 set) | 82 | **0.5471** |
| v1 + signed_v2 | 102 | **0.5502** |
| signed_v2 only | 20 | **0.5485** |

## Top 20 by gain (v1 + signed_v2 sign model)

| rank | feature | family | gain |
|---|---|---|---|
| 1 | f_d_scale_pct | scales_regime | 60,258 |
| 2 | f_sigma_dist_pct | scales_regime | 58,606 |
| 3 | f_d_scale | scales_regime | 57,253 |
| 4 | f_sigma_600s | scales_regime | 46,948 |
| 5 | f_v_scale_pct | scales_regime | 46,060 |
| 6 | f_cancel_before_touch_rate_l | order_survival | 37,712 |
| 7 | f_liquidity_survival_ratio_l | order_survival | 26,863 |
| 8 | f_order_lifetime_ms_l | order_lifetime | 22,980 |
| 9 | f_iceberg_score_bid_l | iceberg | 22,289 |
| 10 | f_iceberg_asym_l | signed_v2 | 22,043 |
| 11 | f_iceberg_score_ask_l | iceberg | 21,909 |
| 12 | f_aggr_delta_ratio_l | aggr_delta | 21,908 |
| 13 | f_sigma_dist | scales_regime | 21,016 |
| 14 | f_aggr_delta_l_norm | aggr_delta | 20,616 |
| 15 | f_failed_sweeps_l | sweeps | 18,994 |
| 16 | f_order_lifetime_chain_adj_ms_l | order_lifetime | 18,820 |
| 17 | f_failed_sweep_net_ratio_l | signed_v2 | 16,209 |
| 18 | f_hidden_fill_imbalance_l | signed_v2 | 16,016 |
| 19 | f_aggr_delta_l | aggr_delta | 14,214 |
| 20 | f_spread_ticks_pctile | spread | 12,671 |

Runtime: 0.8 min
