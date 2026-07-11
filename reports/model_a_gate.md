# Step 20 — Model A GO/NO-GO gate

Generated: 2026-07-11T16:40:04+00:00 | criteria: config `[gate]` (registered 2026-07-11, before training)
| OOS: 2024-01-02 .. 2024-03-28 (75 sessions, 2,982,652 rows, full resolution)

## VERDICT: **NO-GO** — no combination met all pre-registered criteria

| combo | train rows | eff-N | AUC(bull) | AUC(bear) | AUC mean | Brier vs base | trades | sessions | expectancy (pts) | wo best sess | wo releases | criteria | result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 12m/unweighted | 3,507,363 | 183,279 | 0.8759 | 0.8778 | **0.8768** | +5.526% | 298,266 | 74 | **-0.1967** | -0.1967 | -0.1961 | ✗ ✓ ✓ ✗ ✗ | fail |
| 12m/uniq-weighted | 3,507,363 | 183,279 | 0.8765 | 0.8784 | **0.8775** | +5.519% | 298,266 | 71 | **-0.1993** | -0.1993 | -0.1988 | ✗ ✓ ✓ ✗ ✗ | fail |
| 3y/unweighted | 10,525,002 | 551,089 | 0.8781 | 0.8791 | **0.8786** | +5.702% | 298,266 | 73 | **-0.1982** | -0.1983 | -0.1978 | ✗ ✓ ✓ ✗ ✗ | fail |
| 3y/uniq-weighted | 10,525,002 | 551,089 | 0.8788 | 0.8798 | **0.8793** | +5.709% | 298,266 | 74 | **-0.1965** | -0.1968 | -0.1958 | ✗ ✓ ✓ ✗ ✗ | fail |
| full/unweighted | 22,909,842 | 1,143,584 | 0.8791 | 0.8797 | **0.8794** | +5.727% | 298,266 | 74 | **-0.1930** | -0.1932 | -0.1929 | ✗ ✓ ✓ ✗ ✗ | fail |
| full/uniq-weighted | 22,909,842 | 1,143,584 | 0.8796 | 0.8802 | **0.8799** | +5.706% | 298,266 | 74 | **-0.1964** | -0.1964 | -0.1955 | ✗ ✓ ✓ ✗ ✗ | fail |

Criteria order: expectancy>0 & ≥20 sessions · AUC ≥ 0.55 · BrierΔ ≥ 2% · robust w/o best session · robust w/o release days.

## Feature importance (best combo: full/uniq-weighted)

| rank | feature | family | gain | SHAP mean\|c\| | perm ΔAUC |
|---|---|---|---|---|---|
| 1 | f_aggr_delta_ratio_l | aggr_delta | 2,801 | 0.10984 | +0.00910 |
| 2 | f_failed_sweeps_l | sweeps | 145,944 | 0.10243 | +0.00272 |
| 3 | f_sigma_dist | scales_regime | 19,674 | 0.10074 | +0.00087 |
| 4 | f_spread_ticks_pctile | spread | 3,684 | 0.09727 | +0.00200 |
| 5 | f_sigma_30s | scales_regime | 94,242 | 0.08400 | +0.01183 |
| 6 | f_d_scale | scales_regime | 7,401 | 0.08145 | +0.00035 |
| 7 | f_aggr_delta_l | aggr_delta | 3,633 | 0.08061 | +0.00477 |
| 8 | f_queue_turnover_bid_m | queue_turnover | 6,747 | 0.06637 | +0.00219 |
| 9 | f_iceberg_score_bid_l | iceberg | 11,514 | 0.06248 | +0.00140 |
| 10 | f_iceberg_score_ask_l | iceberg | 11,094 | 0.05553 | +0.00127 |
| 11 | f_queue_turnover_ask_m | queue_turnover | 6,681 | 0.05201 | +0.00195 |
| 12 | f_book_imbalance_total | book_imbalance | 1,859 | 0.03355 | +0.00044 |
| 13 | f_order_lifetime_ms_l | order_lifetime | 2,146 | 0.03297 | +0.00154 |
| 14 | f_stacking_bid_m | stacking | 1,145 | 0.02937 | +0.00058 |
| 15 | f_liquidity_survival_ratio_l | order_survival | 4,414 | 0.02895 | +0.00076 |
| 16 | f_trade_burst_intensity_s_pctile | trade_burst | 617 | 0.02727 | +0.00132 |
| 17 | f_aggr_delta_l_norm | aggr_delta | 1,636 | 0.02718 | +0.00191 |
| 18 | f_trade_burst_intensity_s | trade_burst | 742 | 0.02573 | +0.00042 |
| 19 | f_liquidity_age_bid_s_rz | liquidity_age | 917 | 0.02506 | +0.00092 |
| 20 | f_stacking_ask_m | stacking | 1,020 | 0.02483 | +0.00060 |
| 21 | f_sigma_600s | scales_regime | 5,120 | 0.02383 | +0.00062 |
| 22 | f_liquidity_age_bid_s | liquidity_age | 826 | 0.02256 | +0.00048 |
| 23 | f_absorption_net_s_pctile | absorption | 377 | 0.02189 | +0.00031 |
| 24 | f_cancel_before_touch_rate_l | order_survival | 5,358 | 0.02175 | +0.00241 |
| 25 | f_liquidity_age_ask_s | liquidity_age | 883 | 0.02136 | +0.00043 |

## Per-family ablations (12m variant, OOS mean-AUC drop when family removed)

| family | ΔAUC |
|---|---|
| scales_regime | +0.0020 |
| order_survival | +0.0013 |
| order_lifetime | +0.0010 |
| sweeps | +0.0009 |
| queue_turnover | +0.0008 |
| aggr_delta | +0.0005 |
| liquidity_age | +0.0005 |
| price_progress | +0.0002 |
| queue_depletion | +0.0002 |
| iceberg | +0.0002 |
| vacuum | +0.0002 |
| mlofi | +0.0001 |
| trade_burst | +0.0001 |
| price_impact | +0.0001 |
| book_imbalance | +0.0001 |
| resiliency | +0.0000 |
| microprice | +0.0000 |
| absorption | -0.0000 |
| stacking | -0.0000 |
| replenishment | -0.0001 |
| spread | -0.0002 |

Runtime: 88 min | model params: fixed (src/models/model_a.py) | expectancy proxy + thinning: config [gate].
## Post-verdict diagnostic (appended after the gate decision; not a criterion)

Decomposing the headline AUC on the 12m/unweighted model:

| skill | AUC |
|---|---|
| directional-move vs NO_TRADE (2.98M OOS rows) | **0.8798** |
| bull vs bear, GIVEN a directional move (56,176 rows) | **0.5558** |

The model has learned to predict WHEN a cost-beating move is imminent
(volatility/activity — consistent with sigma/spread/turnover dominating
the importance tables) but carries almost no SIGN information. Gross
top-decile expectancy ≈ +0.003 pts vs the 0.2 pt round-trip cost. This is
the precise iteration target the plan's gate discipline exists to expose.
