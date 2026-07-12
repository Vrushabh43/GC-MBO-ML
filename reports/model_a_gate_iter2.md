# Step 20 — Model A GO/NO-GO gate

Generated: 2026-07-12T10:41:38+00:00 | criteria: config `[gate]` (registered 2026-07-11, before training)
| OOS: 2024-01-02 .. 2024-03-28 (75 sessions, 2,982,652 rows, full resolution)

## VERDICT: **NO-GO** — no combination met all pre-registered criteria

| combo | train rows | eff-N | AUC(bull) | AUC(bear) | AUC mean | Brier vs base | trades | sessions | expectancy (pts) | wo best sess | wo releases | criteria | result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 12m/unweighted | 3,507,363 | 183,279 | 0.8762 | 0.8779 | **0.8771** | +5.510% | 298,266 | 75 | **-0.1964** | -0.1964 | -0.1961 | ✗ ✓ ✓ ✗ ✗ | fail |
| 12m/uniq-weighted | 3,507,363 | 183,279 | 0.8767 | 0.8788 | **0.8778** | +5.530% | 298,266 | 72 | **-0.1984** | -0.1984 | -0.1982 | ✗ ✓ ✓ ✗ ✗ | fail |
| 3y/unweighted | 10,525,002 | 551,089 | 0.8781 | 0.8791 | **0.8786** | +5.731% | 298,266 | 73 | **-0.1973** | -0.1975 | -0.1975 | ✗ ✓ ✓ ✗ ✗ | fail |
| 3y/uniq-weighted | 10,525,002 | 551,089 | 0.8788 | 0.8797 | **0.8793** | +5.711% | 298,266 | 73 | **-0.1970** | -0.1972 | -0.1971 | ✗ ✓ ✓ ✗ ✗ | fail |
| full/unweighted | 22,909,842 | 1,143,584 | 0.8789 | 0.8797 | **0.8793** | +5.677% | 298,266 | 74 | **-0.1919** | -0.1922 | -0.1916 | ✗ ✓ ✓ ✗ ✗ | fail |
| full/uniq-weighted | 22,909,842 | 1,143,584 | 0.8790 | 0.8801 | **0.8796** | +5.679% | 298,266 | 73 | **-0.1973** | -0.1975 | -0.1971 | ✗ ✓ ✓ ✗ ✗ | fail |

Criteria order: expectancy>0 & ≥20 sessions · AUC ≥ 0.55 · BrierΔ ≥ 2% · robust w/o best session · robust w/o release days.

## Feature importance (best combo: full/uniq-weighted)

| rank | feature | family | gain | SHAP mean\|c\| | perm ΔAUC |
|---|---|---|---|---|---|
| 1 | f_aggr_delta_ratio_l | aggr_delta | 2,700 | 0.10787 | +0.00845 |
| 2 | f_failed_sweeps_l | sweeps | 120,931 | 0.10785 | +0.00203 |
| 3 | f_spread_ticks_pctile | spread | 3,617 | 0.09116 | +0.00165 |
| 4 | f_sigma_dist | scales_regime | 25,638 | 0.08888 | +0.00081 |
| 5 | f_d_scale | scales_regime | 8,278 | 0.08388 | +0.00045 |
| 6 | f_sigma_30s | scales_regime | 101,455 | 0.08091 | +0.01134 |
| 7 | f_aggr_delta_l | aggr_delta | 3,458 | 0.07754 | +0.00402 |
| 8 | f_queue_turnover_bid_m | queue_turnover | 7,499 | 0.06581 | +0.00213 |
| 9 | f_iceberg_score_bid_l | iceberg | 15,122 | 0.05848 | +0.00141 |
| 10 | f_queue_turnover_ask_m | queue_turnover | 7,690 | 0.05512 | +0.00219 |
| 11 | f_iceberg_score_ask_l | iceberg | 15,272 | 0.05173 | +0.00112 |
| 12 | f_order_lifetime_ms_l | order_lifetime | 2,308 | 0.03608 | +0.00175 |
| 13 | f_liquidity_survival_ratio_l | order_survival | 4,468 | 0.02830 | +0.00077 |
| 14 | f_stacking_bid_m | stacking | 1,019 | 0.02744 | +0.00046 |
| 15 | f_aggr_delta_l_norm | aggr_delta | 1,598 | 0.02731 | +0.00143 |
| 16 | f_sigma_120s | scales_regime | 13,007 | 0.02464 | +0.00020 |
| 17 | f_trade_burst_intensity_s_pctile | trade_burst | 571 | 0.02447 | +0.00079 |
| 18 | f_stacking_ask_m | stacking | 926 | 0.02413 | +0.00050 |
| 19 | f_book_imbalance_total | book_imbalance | 1,308 | 0.02389 | +0.00010 |
| 20 | f_liquidity_age_bid_s_rz | liquidity_age | 902 | 0.02368 | +0.00080 |
| 21 | f_liquidity_age_ask_s | liquidity_age | 825 | 0.02287 | +0.00046 |
| 22 | f_liquidity_age_bid_s | liquidity_age | 820 | 0.02239 | +0.00056 |
| 23 | f_sigma_600s | scales_regime | 3,742 | 0.02205 | +0.00020 |
| 24 | f_trade_burst_intensity_s | trade_burst | 656 | 0.02162 | +0.00032 |
| 25 | f_mlofi_near_m_norm | mlofi | 1,553 | 0.02112 | +0.00039 |

## Per-family ablations (12m variant, OOS mean-AUC drop when family removed)

| family | ΔAUC |
|---|---|
| scales_regime | +0.0021 |
| order_survival | +0.0014 |
| order_lifetime | +0.0011 |
| sweeps | +0.0011 |
| liquidity_age | +0.0006 |
| queue_turnover | +0.0005 |
| aggr_delta | +0.0005 |
| book_imbalance | +0.0004 |
| spread | +0.0003 |
| price_impact | +0.0002 |
| mlofi | +0.0002 |
| stacking | +0.0002 |
| microprice | +0.0002 |
| signed_v2 | +0.0002 |
| vacuum | +0.0002 |
| trade_burst | +0.0002 |
| resiliency | +0.0002 |
| replenishment | +0.0001 |
| iceberg | +0.0001 |
| absorption | +0.0001 |
| price_progress | +0.0000 |
| queue_depletion | +0.0000 |

Runtime: 103 min | model params: fixed (src/models/model_a.py) | expectancy proxy + thinning: config [gate].

## Post-verdict diagnostic (appended after the gate decision; not a criterion)

Decomposing the headline AUC on the 12m/unweighted model, side by side with
Step 20:

| skill | Step 20 (v1 features) | iteration 2 (v1 + signed_v2) |
|---|---|---|
| directional-move vs NO_TRADE (2.98M OOS rows) | 0.8798 | **0.8799** |
| bull vs bear, GIVEN a directional move (56,176 rows) | 0.5558 | **0.5590** |

The signed-asymmetry set moved sign skill by +0.003 AUC — the same delta the
dedicated binary sign harness measured (reports/sign_diagnostic_*.md), and
far short of what the economics need. The sign-harness probes localize the
constraint: full-history training lifts binary sign AUC to 0.5797 (data,
not features, is binding at 30 s), sign skill DECAYS with horizon (h120
0.525–0.531 < h30), and the 20 signed_v2 features alone reproduce ~all
measurable sign signal (0.5679 full-history) — the information is now
concentrated, but it is the same information. Feature-engineering within
the current per-group MBO primitive set is exhausted as a sign lever.
