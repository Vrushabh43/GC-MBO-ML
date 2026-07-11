"""Step 19 driver — build the sample index for the dev slice (or a given
date range) and write the effective-sample-size report the plan requires
for every training set.

Parallel (one session per worker, hardware profile ~10 workers) and
RESUMABLE: existing outputs are skipped unless --force. Per-session
summaries are cached beside the parquet so the report can always be
rebuilt without recomputation.

Run:  .venv/bin/python scripts/build_sample_index.py [START END] [--force] [workers=N]
Writes data/sample_index/samples-YYYYMMDD.parquet (+ .summary.json) per
session and reports/sample_index.md.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from databento_io.sessions import list_session_dates  # noqa: E402
from utilities.config import load_config  # noqa: E402


def build_one(date_iso: str) -> tuple[str, dict]:
    """Worker: one session -> parquet + summary sidecar."""
    from datasets.sample_index import write_session_samples

    cfg = load_config()
    date = dt.date.fromisoformat(date_iso)
    p, _, summary = write_session_samples(date, cfg)
    Path(str(p).replace(".parquet", ".summary.json")).write_text(
        json.dumps(summary, default=str)
    )
    return date_iso, summary


def main() -> int:
    cfg = load_config()
    args = [a for a in sys.argv[1:] if not a.startswith("--") and "=" not in a]
    force = "--force" in sys.argv
    workers = next(
        (int(a.split("=")[1]) for a in sys.argv[1:] if a.startswith("workers=")),
        cfg.performance.batch_workers,
    )
    if len(args) >= 2:
        start, end = (dt.date.fromisoformat(a) for a in args[:2])
    else:
        start, end = cfg.data.dev_slice_start, cfg.data.dev_slice_end
    dates = list_session_dates(start, end, cfg)

    out_dir = REPO / cfg.raw["samples"]["sample_index_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    todo, summaries = [], {}
    for d in dates:
        sidecar = out_dir / f"samples-{d.strftime('%Y%m%d')}.summary.json"
        if sidecar.exists() and not force:
            summaries[str(d)] = json.loads(sidecar.read_text())
        else:
            todo.append(d)
    print(f"{len(dates)} sessions, {len(todo)} to build, {workers} workers"
          + (" (force)" if force else ""))

    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(build_one, str(d)): d for d in todo}
        for fut in as_completed(futs):
            date_iso, s = fut.result()
            summaries[date_iso] = s
            print(f"{date_iso}: {s['picked']:,} samples, "
                  f"h30 eff-N {s['h30']['effective_n']:,.0f}")

    # ---------------- report ------------------------------------------------
    lines = [
        "# Sample index — effective-sample-size report (Step 19)",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
        f"Sessions: {dates[0]} .. {dates[-1]} ({len(dates)}) | "
        "weights: López-de-Prado average uniqueness, per horizon",
        "",
        "| session | picked | clock | event-trig | h30 train | **h30 eff-N** | "
        "h30 NT/BU/BE | h120 eff-N | h600 eff-N |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    EMPTY = {"trainable": 0, "effective_n": 0.0,
             "class_balance": {"no_trade": float("nan"), "bullish": float("nan"),
                               "bearish": float("nan")}}
    tot = {30: [0, 0.0], 120: [0, 0.0], 600: [0, 0.0]}
    meta = None
    for d in dates:
        s = summaries[str(d)]
        for h in (30, 120, 600):  # zero-sample sessions (legacy sidecars)
            s.setdefault(f"h{h}", dict(EMPTY))
        meta = s["label_metadata"]
        by = s["by_trigger"]
        ev = sum(v for k, v in by.items() if k != "clock")
        cb = s["h30"]["class_balance"]
        lines.append(
            f"| {d} | {s['picked']:,} | {by.get('clock', 0):,} | {ev:,} "
            f"| {s['h30']['trainable']:,} | **{s['h30']['effective_n']:,.0f}** "
            f"| {cb['no_trade']:.2f}/{cb['bullish']:.2f}/{cb['bearish']:.2f} "
            f"| {s['h120']['effective_n']:,.0f} | {s['h600']['effective_n']:,.0f} |"
        )
        for h in (30, 120, 600):
            tot[h][0] += s[f"h{h}"]["trainable"]
            tot[h][1] += s[f"h{h}"]["effective_n"]
    lines += [
        "",
        "## Totals — row count is NOT information count (plan Phase 6)",
        "",
        "| horizon | trainable rows | effective N | effective share |",
        "|---|---|---|---|",
    ]
    for h in (30, 120, 600):
        n, e = tot[h]
        lines.append(f"| {h}s | {n:,} | {e:,.0f} | {e / max(n, 1):.1%} |")
    lines += [
        "",
        f"Label convention (stored with every file): `{meta}`",
        "",
        "Uniqueness weights are per-horizon columns in each parquet; Phase 8",
        "training uses them (optionally) and Phase 9 purging uses",
        "`label_end_ts`. Event-trigger caps and the minimum-spacing rule are",
        "config `[samples]`.",
        "",
    ]
    out = REPO / "reports" / "sample_index.md"
    out.write_text("\n".join(lines))
    print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
