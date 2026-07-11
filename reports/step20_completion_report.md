# Step 20 Completion Report — Model A Baseline and the GO/NO-GO Gate

Date: 2026-07-11 | Spec: `gc_orderflow_plan_v2.md` v2.5 | Build Order Step 20 (plan Phase 8)

## Verdict: **STEP COMPLETE — GATE RESULT: NO-GO**

The gate was executed exactly as the plan demands: criteria pre-registered
in config before any training, evaluated on purged+embargoed out-of-sample
data, with all required deliverables. No combination met the economic
criteria, so per the plan: **iterate on features, labels, and sampling —
do NOT escalate to Models B/C.** The criteria were not touched after
seeing results (config carries the registration + an appended result
block).

---

## 1. What was run

- **Pre-registration first** (config `[gate]`, 2026-07-11): OOS =
  2024-01-02..2024-03-28 (75 sessions inside the frozen validate year;
  test-2025/holdout untouched); five criteria (expectancy>0 on ≥20
  sessions after the 2-tick cost · AUC ≥ 0.55 · Brier ≥2% over baseline ·
  robust to best-session removal · robust to release-day removal); fixed
  LightGBM hyperparameters and seed; data-prep methodology appended
  before training.
- **Gate dataset**: sample index extended to capture the 83-feature
  engineered vector at each sample instant (`f_mid_ticks` excluded as an
  era proxy), then built for 2017-05-21..2024-03-28 — **2,146 sessions,
  85.4M trainable 30 s rows (effective N 4.31M), zero gaps**, front
  contract from the verified roll ledger.
- **Six combinations**: {12m, 3y, full-history} × {unweighted,
  uniqueness-weighted}, each purged and embargoed against the OOS start;
  OOS evaluated at full resolution (2.98M rows). Runtime 88 min.

## 2. Gate results (`reports/model_a_gate.md`)

| combo | AUC mean | Brier vs base | expectancy (pts/trade) | result |
|---|---|---|---|---|
| 12m (unw / uniq) | 0.8768 / 0.8775 | +5.5% | −0.197 / −0.199 | fail |
| 3y (unw / uniq) | 0.8786 / 0.8793 | +5.7% | −0.198 / −0.197 | fail |
| full (unw / uniq) | 0.8794 / **0.8799** | +5.7% | **−0.193** / −0.196 | fail |

Every combination passes the statistical criteria decisively and fails
the economic ones identically. More training history helps monotonically
on both axes (the plan's 12m/3y/full experiment has its answer: full
history is best, but the difference is small).

## 3. The diagnosis (the most valuable output of Step 20)

Decomposing the discrimination on the OOS set:

- **Move-timing skill: AUC 0.880** (directional-move vs NO_TRADE) — the
  order-flow features predict *that* a cost-beating 30 s move is imminent
  extremely well; importance is led by regime scales (sigma), spread
  percentile, failed sweeps, delta ratio, queue turnover, iceberg scores.
- **Sign skill: AUC 0.556** (bull vs bear, given a directional move) —
  barely above chance. Gross top-decile expectancy ≈ +0.003 pts vs the
  0.2 pt round trip.

The model is a good *volatility-event detector* and a poor *direction
picker*. Ablations agree: no single family is load-bearing (max ΔAUC
+0.002, scales_regime), i.e., the timing signal is broad and redundant
while sign-carrying information is thin everywhere.

## 4. Plan-mandated next actions (iteration, not escalation)

1. **Sign-carrying features**: the current set is dominated by magnitude/
   activity measures. Directional candidates already in the data: signed
   MLOFI at finer windows, signed absorption asymmetry, iceberg side
   asymmetry, event-window order-book slope, signed sweep-reclaim
   direction — engineered and unit-tested through the same Phase 3
   discipline before any retrain.
2. **Sampling**: the 298k top-decile "trades" are overwhelmingly
   overlapping quiet-period clock samples around volatility events;
   event-conditional sampling (or separating the move-timing and sign
   problems explicitly) concentrates training signal on the decision that
   actually earns money.
3. **Labels**: the 30 s gate horizon stands (registered), but the
   dual-unit labels for 2 m already exist if iteration wants confirmation
   that sign skill grows with horizon before re-attacking 30 s.
4. Re-run the gate **unchanged** after each iteration; the criteria file
   stays frozen.

## 5. Files created or changed

| File | Change |
|---|---|
| `config/config.toml` | `[gate]` pre-registration + appended result block |
| `src/models/model_a.py` | **new** — Model A trainer, dependency-free metrics, gate evaluation, importance/SHAP/permutation, memory-slim loader |
| `scripts/run_gate.py` | **new** — full gate runner + report writer |
| `src/datasets/sample_index.py` | feature capture at sample instants; robust empty-session summaries |
| `src/features/flow_stream.py` | front contract from the roll ledger |
| `scripts/build_sample_index.py` | legacy-sidecar guards |
| `tests/test_model_a.py` | **new** — 10 tests (metric known-answers incl. oracle-pass/uninformed-fail/concentration-catch, thinning, family coverage, end-to-end, determinism) |
| `reports/model_a_gate.md` | gate report + post-verdict diagnostic appendix |
| `data/sample_index/` | 2,146 gate-period session indexes (30 GB, derived) |

## 6. Tests

Full suite **237/237 PASS** (227 + 10 Model A). Engineering incidents
fixed en route: zero-sample sessions produced schema-less parquets and
summary rows (loader + summary hardened); the full-history load at all
150 columns OOM-killed a 64 GB box (column-restricted, dtype-slimmed
loader; ~4× smaller).

## 7. Honest limitations

1. The expectancy proxy (mid-based 30 s return minus full round trip,
   fired on the top decile of ALL samples) is deliberately blunt and was
   registered as such. A NO-GO under it is meaningful; a future PASS under
   it will be conservative.
2. Sunday sessions count toward the ≥20-session requirement (they carry
   real trades but thin ones).
3. Macro calendar tag-tier gaps (GDP/PCE/auctions) mean "release days"
   in the robustness criterion covers CPI/NFP/FOMC/PPI days only.
