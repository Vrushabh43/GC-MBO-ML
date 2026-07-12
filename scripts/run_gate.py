"""Step 20 — train Model A and evaluate the pre-registered GO/NO-GO gate.

Reads EVERYTHING from config [gate] (criteria registered 2026-07-11,
before training). Trains the three window variants (12m / 3y / full
history) x two weighting modes on purged+embargoed data, evaluates each
on the frozen-validate OOS period, and writes reports/model_a_gate.md
with the verdict plus the plan-required deliverables: gain + permutation
importance, native TreeSHAP on top features, and per-family ablations.

Run:  .venv/bin/python scripts/run_gate.py [--no-ablations] [--no-permutation]
      [--out=NAME.md]   (default model_a_gate.md; gate iterations write a
                         fresh report so every run's verdict stays on disk)
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import numpy as np  # noqa: E402

from databento_io.sessions import list_session_dates  # noqa: E402
from evaluation.splits import purge_train_samples  # noqa: E402
from models.model_a import (  # noqa: E402
    FAMILIES,
    evaluate_gate,
    family_of,
    feature_columns,
    gain_importance,
    load_index_frame,
    permutation_auc_drop,
    shap_top_features,
    train_model_a,
)
from utilities.config import load_config  # noqa: E402

NS = 1_000_000_000


def main() -> int:
    t_start = time.time()
    cfg = load_config()
    g = cfg.raw["gate"]
    do_abl = "--no-ablations" not in sys.argv
    do_perm = "--no-permutation" not in sys.argv

    oos_dates = list_session_dates(g["oos_start"], g["oos_end"], cfg)
    variants = {
        "12m": g["variant_12m_start"],
        "3y": g["variant_3y_start"],
        "full": g["variant_full_start"],
    }
    thin = int(g["train_max_clock_rows_per_session"])

    print(f"OOS: {oos_dates[0]} .. {oos_dates[-1]} ({len(oos_dates)} sessions)")
    oos = load_index_frame(oos_dates, cfg)  # FULL resolution (registered)
    eval_start_ts = int(oos["ts"].min())
    print(f"OOS rows: {len(oos):,}")

    results = []
    trained = {}
    for vname, vstart in variants.items():
        dates = list_session_dates(vstart, g["train_end"], cfg)
        t0 = time.time()
        train = load_index_frame(dates, cfg, thin_clock=thin)
        train, audit = purge_train_samples(train, eval_start_ts, cfg, 30)
        freq = np.bincount(
            train["h30_direction"].astype(int), minlength=3
        ).astype(float)
        freq /= freq.sum()
        print(f"[{vname}] {len(dates)} sessions, {audit['kept']:,} rows "
              f"(eff-N {audit['h30_effective_n']:,.0f}) loaded in {time.time()-t0:.0f}s")
        for weighted in (False, True):
            combo = f"{vname}/{'uniq-weighted' if weighted else 'unweighted'}"
            t1 = time.time()
            model, feats = train_model_a(train, cfg, weighted)
            proba = model.predict(oos[feats].to_numpy(dtype=np.float32))
            ev = evaluate_gate(proba, oos, freq, cfg, combo)
            results.append((ev, audit))
            trained[combo] = (model, feats)
            print(f"  {combo}: AUC {ev.auc_mean:.4f} | brierΔ "
                  f"{ev.brier_improvement:+.3%} | exp {ev.expectancy_pts:+.4f} pts "
                  f"({ev.n_trades:,} trades/{ev.trade_sessions} sess) | "
                  f"{'PASS' if ev.passed else 'fail'} | {time.time()-t1:.0f}s")
        del train

    passed = [ev for ev, _ in results if ev.passed]
    best = max((ev for ev, _ in results), key=lambda e: e.auc_mean)
    verdict = "GO" if passed else "NO-GO"

    # deliverables on the best-AUC combo
    model, feats = trained[best.combo]
    gain = gain_importance(model, feats)
    shap = shap_top_features(model, oos, feats)
    perm = permutation_auc_drop(model, oos, feats, cfg) if do_perm else None

    abl_rows = []
    if do_abl:
        # ablations on the 12m variant (cheapest; documented) — drop each
        # family, retrain, measure OOS mean-AUC degradation
        dates = list_session_dates(variants["12m"], g["train_end"], cfg)
        train = load_index_frame(dates, cfg, thin_clock=thin)
        train, _ = purge_train_samples(train, eval_start_ts, cfg, 30)
        base_model, base_feats = train_model_a(train, cfg, False)
        base_p = base_model.predict(oos[base_feats].to_numpy(dtype=np.float32))
        y = oos["h30_direction"].to_numpy().astype(int)
        from models.model_a import rank_auc

        base_auc = float(np.mean([rank_auc(y == 1, base_p[:, 1]),
                                  rank_auc(y == 2, base_p[:, 2])]))
        for fam in FAMILIES:
            keep = [f for f in base_feats if family_of(f) != fam]
            m2, _ = train_model_a(train, cfg, False, features=keep)
            p2 = m2.predict(oos[keep].to_numpy(dtype=np.float32))
            auc2 = float(np.mean([rank_auc(y == 1, p2[:, 1]),
                                  rank_auc(y == 2, p2[:, 2])]))
            abl_rows.append((fam, base_auc - auc2))
            print(f"  ablate {fam:16s} ΔAUC {base_auc - auc2:+.4f}")
        abl_rows.sort(key=lambda r: -r[1])

    # ------------------------------ report ---------------------------------
    lines = [
        "# Step 20 — Model A GO/NO-GO gate",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} "
        f"| criteria: config `[gate]` (registered 2026-07-11, before training)",
        f"| OOS: {oos_dates[0]} .. {oos_dates[-1]} ({len(oos_dates)} sessions, "
        f"{len(oos):,} rows, full resolution)",
        "",
        f"## VERDICT: **{verdict}**"
        + (f" — {len(passed)} combination(s) passed all criteria" if passed
           else " — no combination met all pre-registered criteria"),
        "",
        "| combo | train rows | eff-N | AUC(bull) | AUC(bear) | AUC mean | "
        "Brier vs base | trades | sessions | expectancy (pts) | wo best sess | "
        "wo releases | criteria | result |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for ev, audit in results:
        crit = " ".join("✓" if v else "✗" for v in ev.criteria.values())
        lines.append(
            f"| {ev.combo} | {audit['kept']:,} | {audit['h30_effective_n']:,.0f} "
            f"| {ev.auc_bull:.4f} | {ev.auc_bear:.4f} | **{ev.auc_mean:.4f}** "
            f"| {ev.brier_improvement:+.3%} | {ev.n_trades:,} | {ev.trade_sessions} "
            f"| **{ev.expectancy_pts:+.4f}** | {ev.expectancy_wo_best_session:+.4f} "
            f"| {ev.expectancy_wo_release_days:+.4f} | {crit} "
            f"| {'**PASS**' if ev.passed else 'fail'} |"
        )
    lines += [
        "",
        "Criteria order: expectancy>0 & ≥20 sessions · AUC ≥ 0.55 · "
        "BrierΔ ≥ 2% · robust w/o best session · robust w/o release days.",
        "",
        f"## Feature importance (best combo: {best.combo})",
        "",
        "| rank | feature | family | gain | SHAP mean\\|c\\| | perm ΔAUC |",
        "|---|---|---|---|---|---|",
    ]
    for k, (f, v) in enumerate(shap.head(25).items(), 1):
        lines.append(
            f"| {k} | {f} | {family_of(f)} | {gain.get(f, 0):,.0f} | {v:.5f} "
            f"| {perm.get(f, float('nan')):+.5f} |" if perm is not None else
            f"| {k} | {f} | {family_of(f)} | {gain.get(f, 0):,.0f} | {v:.5f} | — |"
        )
    if abl_rows:
        lines += [
            "",
            "## Per-family ablations (12m variant, OOS mean-AUC drop when family removed)",
            "",
            "| family | ΔAUC |",
            "|---|---|",
        ]
        for fam, d in abl_rows:
            lines.append(f"| {fam} | {d:+.4f} |")
    lines += [
        "",
        f"Runtime: {(time.time()-t_start)/60:.0f} min | model params: fixed "
        "(src/models/model_a.py) | expectancy proxy + thinning: config [gate].",
        "",
    ]
    out_name = next(
        (a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--out=")),
        "model_a_gate.md",
    )
    out = REPO / "reports" / out_name
    out.write_text("\n".join(lines))
    print(f"\nVERDICT: {verdict}\nreport -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
