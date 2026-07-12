# Gate Iteration 2 Completion Report — Sign-Carrying Features (signed_v2)

Date: 2026-07-12 | Spec: `gc_orderflow_plan_v2.md` v2.5, Phase 8 gate rule
("If the gate fails: iterate on features, labels, and sampling — do not
escalate") | Follows: `reports/step20_completion_report.md` (NO-GO)

## Verdict: **ITERATION COMPLETE — GATE RESULT: NO-GO (unchanged)**

The Step 20 diagnosis said the model times volatility events (AUC 0.880)
but cannot pick direction (sign AUC 0.556). This iteration attacked the
sign deficit with 20 purpose-built signed features, measured the result
with a dedicated sign harness, and re-ran the **frozen** gate. The verdict
did not move — and the iteration's real product is the localization of
*why*: at the 30 s horizon, sign information in the current MBO primitive
set tops out near AUC 0.58, far below what a 2-tick round trip demands.

---

## 1. What was built

**signed_v2 feature set** (`src/features/core_features.py`, same
ingest/compose one-code-path discipline, positive = buy/bid-supportive,
all bounded [-1,1] except one sigma-twinned tick distance):

- bounded signed flow-imbalance ratios (net ÷ gross depth flow): level-1
  and near-band, 2 s / 10 s windows;
- execution-side imbalances: displayed fills (2 s / 10 s), hidden fills
  (60 s), iceberg-score side asymmetry;
- liquidity-motion tilts: pull imbalance at/near the touch, vacuum tilt,
  resiliency tilt, queue-depletion tilt, replenishment tilt, turnover tilt;
- book-shape signs: outer-band imbalance, near-vs-outer imbalance tilt,
  depth-concentration tilt;
- signed sweep set: net swept ticks (+`_norm` sigma twin), signed
  reclaim-against-sweep score, failed-sweep direction ratio.

Tests: 12 new known-answer/property tests (sign correctness by construction
per scenario, boundedness, neutral values, 3× volume invariance, ablation-
family mapping). Full suite **247/247** (pre-iteration baseline 235 — the
"237" in the Step 20 report was a miscount by 2).

**Sign-skill harness** (`scripts/run_sign_diagnostic.py`): binary
bull-vs-bear LightGBM on directional rows only, purged+embargoed like the
gate, minutes per probe — the fast loop the gate itself is too heavy for.
Sample index force-rebuilt for 2017-05-21..2024-03-28 + dev slice
(2,158 sessions; labels verified byte-identical, only feature columns
added).

## 2. Evidence chain (reports/sign_diagnostic_h*.md)

| probe | v1 (82 feats) | v1+signed_v2 (102) | signed_v2 only (20) |
|---|---|---|---|
| h30, 12 m train (245k dir. rows) | 0.5471 | 0.5502 | 0.5485 |
| h30, full history (1.59M) | **0.5797** | 0.5785 | 0.5679 |
| h120, 12 m train (1.35M) | 0.5045 | 0.5041 | 0.5204 |
| h120, full history (7.9M) | 0.5255 | 0.5283 | 0.5315 |

Three findings, each decision-relevant:

1. **Features are not the binding constraint — data is.** signed_v2 adds
   ≤ +0.003 AUC on top of v1 at h30, while full-history training adds
   +0.033. The 20 signed features alone reproduce essentially all sign
   signal of the 82 v1 features (concentration without new information).
2. **Sign skill decays with horizon** (h120 < h30 everywhere). The Step 20
   speculation that sign might grow with horizon is refuted; 30 s stands.
3. **The realistic sign ceiling of this information set is ≈ 0.58** (full
   history, all directional rows, dedicated binary objective). The
   multiclass gate model achieves 0.559 of it (dilution by the ~98%
   NO_TRADE objective accounts for the rest — see §4 sampling note).

## 3. Frozen gate re-run (`reports/model_a_gate_iter2.md`)

Identical criteria, data prep, seed, thinning; only the feature columns
changed. All six combos: statistical PASS (AUC 0.8771–0.8796, BrierΔ
+5.51–5.73%), economic FAIL (expectancy −0.1919..−0.1984 pts/trade after
the 0.2 pt cost) — the same picture as Step 20 to the third decimal.
Ablations: signed_v2 ΔAUC +0.0002 (fully redundant for the mixed
objective). Multiclass decomposition: timing 0.8799 / sign 0.5590 (was
0.8798 / 0.5558). Runtime 103 min.

Config `[gate]` carries an appended ITERATION 2 result block; the
registered criteria remain byte-untouched.

## 4. What this buys the next iteration (plan: sampling / labels next)

- **Sampling**: the binary harness *is* event-conditional training taken to
  the limit, and it recovers 0.580 vs the multiclass's 0.559 — ~0.02 AUC
  is available from training composition alone. But naively oversampling
  directional rows in the gate model would corrupt the registered
  train-frequency Brier baseline (criterion-adjacent), so a defensible
  variant is: keep the multiclass prep frozen, and note that even the
  clean 0.580 ceiling is economically insufficient (see below).
- **Labels**: the gate's economics need the *product* of timing precision,
  sign accuracy and move size to clear 0.2 pt round trip. At sign ≈ 0.58
  the top-decile gross edge was +0.003 pt — two orders of magnitude short.
  No plausible label-geometry change at 30 s closes that by itself.
- **Honest strategic read**: within the current per-group MBO primitive
  set, Model A's information ceiling is the constraint — not model
  capacity (so Models B/C staying off is not merely procedural), not
  feature form, not window length. Candidate *new information* (not new
  transforms): cross-market state (SI/HG/DX/ES, rates), deeper
  per-participant-pattern primitives from the lifecycle store, and the
  macro tag tiers still pending ingestion. Each is a plan-scope decision
  for the user, not a unilateral iteration.

## 5. Files created or changed

| File | Change |
|---|---|
| `src/features/core_features.py` | signed_v2 set (ingest/compose, signed sweep state) |
| `src/features/normalization.py` | `sweep_net_ticks_m` sigma twin |
| `src/models/model_a.py` | `signed_v2` ablation family (first-match ordering) |
| `scripts/run_sign_diagnostic.py` | **new** — binary sign-skill harness (h30/h120, any train window) |
| `scripts/run_gate.py` | `--out=` so iteration verdicts never overwrite prior reports |
| `tests/test_features_synthetic.py` | +11 signed-set tests |
| `tests/test_model_a.py` | +1 family-mapping test |
| `config/config.toml` | appended ITERATION 2 result block (criteria untouched) |
| `reports/model_a_gate_iter2.md` | frozen-gate re-run + decomposition appendix |
| `reports/sign_diagnostic_h{30,120}s_from{2023,2017}.md` | the four probes |
| `data/sample_index/` | rebuilt with 20 new `f_` columns (labels identical) |

## 6. Tests

**247/247 PASS** (235 baseline + 12 new), re-verified against the rebuilt
index. Rebuild verification on 2024-01-15: row count, trainable count,
direction distribution and return sums byte-identical; new columns bounded
and non-degenerate.

## 7. Honest limitations

1. The sign harness trains on directional rows only; its AUCs are not
   directly comparable to a deployable trading rule (no NO_TRADE screen).
   It is a bound: if 0.58 is the ceiling *with* label knowledge of "a move
   happened", the deployable sign skill is at most that.
2. h600 sign skill was not probed (h120 already showed monotone decay).
3. The four probes share one OOS quarter (2024-Q1); the ceiling estimate
   inherits that quarter's regime.
