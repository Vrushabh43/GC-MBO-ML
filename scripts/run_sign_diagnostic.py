"""Gate-iteration sign-skill diagnostic (NOT the gate).

The Step 20 NO-GO decomposed into move-timing AUC 0.880 vs sign AUC 0.556
(bull-vs-bear given a directional move). This harness measures ONLY the
sign skill, cheaply, so feature iterations can be evaluated in minutes
instead of re-running the full frozen gate:

  - training rows: DIRECTIONAL (h30_direction != 0) trainable samples at
    full resolution over a gate training window, purged+embargoed exactly
    like the gate;
  - three binary bull-vs-bear models: v1 features only (the Step 20 set),
    v1 + the signed_v2 iteration set, and signed_v2 alone;
  - metric: rank AUC on the directional OOS rows.

This is a diagnostic instrument. The GO/NO-GO decision only ever comes
from scripts/run_gate.py with the frozen [gate] criteria.

Run:  .venv/bin/python scripts/run_sign_diagnostic.py [--train-start=YYYY-MM-DD]
Writes reports/sign_diagnostic.md.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from databento_io.sessions import list_session_dates  # noqa: E402
from evaluation.splits import purge_train_samples  # noqa: E402
from models.model_a import (  # noqa: E402
    LGB_PARAMS,
    NUM_TREES,
    family_of,
    feature_columns,
    load_index_frame,
    rank_auc,
)
from utilities.config import load_config  # noqa: E402


def load_directional(dates, cfg, h: int = 30) -> pd.DataFrame:
    """Directional trainable rows only, loaded session-by-session so the
    full-resolution scan stays tiny (directional rows are ~0.2-2%/session).
    For h == 30 this reuses the gate loader; other horizons read the same
    per-session parquets directly (the gate loader is h30 by registration)."""
    import pyarrow.parquet as pq

    frames = []
    if h == 30:
        for d in dates:
            try:
                df = load_index_frame([d], cfg)
            except ValueError:
                continue  # session with zero trainable rows (short/degraded)
            df = df[df["h30_direction"] != 0]
            if len(df):
                frames.append(df)
        return pd.concat(frames, ignore_index=True)

    base = REPO / cfg.raw["samples"]["sample_index_dir"]
    for d in dates:
        p = base / f"samples-{d.strftime('%Y%m%d')}.parquet"
        schema = pq.read_schema(p)
        if f"h{h}_trainable" not in schema.names:
            continue
        fcols = [c for c in schema.names if c.startswith("f_")]
        df = pd.read_parquet(p, columns=fcols + [
            "ts", "label_end_ts",
            f"h{h}_trainable", f"h{h}_direction", f"h{h}_uniqueness",
        ])
        df = df[(df[f"h{h}_trainable"] == 1.0) & (df[f"h{h}_direction"] != 0)]
        if df.empty:
            continue
        df[fcols] = df[fcols].astype(np.float32)
        df[f"h{h}_direction"] = df[f"h{h}_direction"].astype(np.int8)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def main() -> int:
    t0 = time.time()
    cfg = load_config()
    g = cfg.raw["gate"]
    train_start = next(
        (dt.date.fromisoformat(a.split("=", 1)[1]) for a in sys.argv[1:]
         if a.startswith("--train-start=")),
        g["variant_12m_start"],
    )
    h = next(
        (int(a.split("=", 1)[1]) for a in sys.argv[1:]
         if a.startswith("--horizon=")),
        30,
    )

    import lightgbm as lgb

    oos_dates = list_session_dates(g["oos_start"], g["oos_end"], cfg)
    train_dates = list_session_dates(train_start, g["train_end"], cfg)
    print(f"h={h}s | train {train_dates[0]}..{train_dates[-1]} "
          f"({len(train_dates)} sessions), "
          f"OOS {oos_dates[0]}..{oos_dates[-1]} ({len(oos_dates)} sessions)")

    oos = load_directional(oos_dates, cfg, h)
    eval_start_ts = int(oos["ts"].min())
    train = load_directional(train_dates, cfg, h)
    train, audit = purge_train_samples(train, eval_start_ts, cfg, h)
    y_tr = (train[f"h{h}_direction"].to_numpy() == 1).astype(int)  # bull=1
    y_oos = (oos[f"h{h}_direction"].to_numpy() == 1).astype(int)
    print(f"directional rows: train {len(train):,} "
          f"(bull {y_tr.mean():.1%}), OOS {len(oos):,} (bull {y_oos.mean():.1%})")

    all_feats = feature_columns(train)
    v2 = [f for f in all_feats if family_of(f) == "signed_v2"]
    v1 = [f for f in all_feats if family_of(f) != "signed_v2"]
    sets = {"v1 (Step 20 set)": v1, "v1 + signed_v2": all_feats, "signed_v2 only": v2}

    params = {k: v for k, v in LGB_PARAMS.items()
              if k not in ("objective", "num_class")}
    params |= {"objective": "binary", "seed": int(g["seed"])}

    rows, models = [], {}
    for name, feats in sets.items():
        t1 = time.time()
        ds = lgb.Dataset(train[feats].to_numpy(dtype=np.float32), label=y_tr,
                         feature_name=feats, free_raw_data=True)
        model = lgb.train(params, ds, num_boost_round=NUM_TREES)
        p = model.predict(oos[feats].to_numpy(dtype=np.float32))
        auc = rank_auc(y_oos == 1, p)
        rows.append((name, len(feats), auc))
        models[name] = (model, feats)
        print(f"  {name:20s} {len(feats):3d} feats | sign AUC {auc:.4f} "
              f"| {time.time()-t1:.0f}s")

    gain = pd.Series(
        models["v1 + signed_v2"][0].feature_importance("gain"),
        index=models["v1 + signed_v2"][1],
    ).sort_values(ascending=False)

    lines = [
        f"# Gate-iteration sign-skill diagnostic (bull vs bear, given a move) "
        f"— h={h}s",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} "
        f"| NOT the gate — the GO/NO-GO verdict only comes from run_gate.py",
        f"| train {train_dates[0]}..{train_dates[-1]} directional rows "
        f"{len(train):,} | OOS {oos_dates[0]}..{oos_dates[-1]} directional rows "
        f"{len(oos):,}",
        f"| Step 20 reference: multiclass implied sign AUC **0.5558**",
        "",
        "| feature set | features | sign AUC (OOS) |",
        "|---|---|---|",
    ]
    for name, n, auc in rows:
        lines.append(f"| {name} | {n} | **{auc:.4f}** |")
    lines += [
        "",
        "## Top 20 by gain (v1 + signed_v2 sign model)",
        "",
        "| rank | feature | family | gain |",
        "|---|---|---|---|",
    ]
    for k, (f, v) in enumerate(gain.head(20).items(), 1):
        lines.append(f"| {k} | {f} | {family_of(f)} | {v:,.0f} |")
    lines += ["", f"Runtime: {(time.time()-t0)/60:.1f} min", ""]
    out = REPO / "reports" / f"sign_diagnostic_h{h}s_from{train_start.year}.md"
    out.write_text("\n".join(lines))
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
