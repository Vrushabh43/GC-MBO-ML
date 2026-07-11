"""Step 19 driver — build the sample index for the dev slice (or a given
date range) and write the effective-sample-size report the plan requires
for every training set.

Run:  .venv/bin/python scripts/build_sample_index.py [START END]
Writes data/sample_index/samples-YYYYMMDD.parquet per session and
reports/sample_index.md.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from databento_io.sessions import list_session_dates  # noqa: E402
from datasets.sample_index import write_session_samples  # noqa: E402
from utilities.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    if len(sys.argv) > 2:
        start, end = (dt.date.fromisoformat(a) for a in sys.argv[1:3])
    else:
        start, end = cfg.data.dev_slice_start, cfg.data.dev_slice_end
    dates = list_session_dates(start, end, cfg)

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
    tot = {30: [0, 0.0], 120: [0, 0.0], 600: [0, 0.0]}
    n_rows = 0
    meta = None
    for d in dates:
        p, df, s = write_session_samples(d, cfg)
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
        n_rows += s["picked"]
        for h in (30, 120, 600):
            tot[h][0] += s[f"h{h}"]["trainable"]
            tot[h][1] += s[f"h{h}"]["effective_n"]
        print(f"{d}: {s['picked']:,} samples -> {p.name}")

    lines += [
        "",
        "## Totals — row count is NOT information count (plan Phase 6)",
        "",
        "| horizon | trainable rows | effective N | effective share |",
        "|---|---|---|---|",
    ]
    for h in (30, 120, 600):
        n, e = tot[h]
        lines.append(f"| {h}s | {n:,} | {e:,.0f} | {e / max(n,1):.1%} |")
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
