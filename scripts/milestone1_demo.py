"""Milestone 1 (plan Phase 1): reconstruct the full book for a session and
show best bid/ask, spread, and top-10 levels with total size and order count
per level — from BOTH the individual-order view and the aggregated
price-level view.

Run:  .venv/bin/python scripts/milestone1_demo.py [YYYY-MM-DD]
Writes reports/milestone1_book.md.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mbo_engine.engine import MboEngine  # noqa: E402
from utilities.config import load_config  # noqa: E402


def fmt_table(levels_lv, levels_ov) -> list[str]:
    out = ["| # | price | size (level view) | orders | size (order view) | orders | match |",
           "|---|-------|------|--------|------|--------|-------|"]
    for i, (lv, ov) in enumerate(zip(levels_lv, levels_ov), 1):
        match = "OK" if lv == ov else "**MISMATCH**"
        out.append(f"| {i} | {lv[0]:.1f} | {lv[1]} | {lv[2]} | {ov[1]} | {ov[2]} | {match} |")
    return out


def main() -> int:
    cfg = load_config()
    date = (dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
            else cfg.data.dev_slice_start)

    e = MboEngine(cfg)
    r = e.replay_date(date)
    front = e.front_instrument()
    sym = e.symbol(front)
    s = e.stats()

    lines = [
        f"# Milestone 1 — full book reconstruction ({date}, end of file)",
        "",
        f"Session file: `{r.path.name}` | {r.records:,} MBO records in "
        f"{r.seconds:.2f}s ({r.events_per_sec/1e6:.2f}M ev/s) | "
        f"digest `{r.digest:#x}` | halted: {e.halted()}",
        "",
        f"Front contract (by traded volume): **{sym}** (instrument_id {front})",
        f"Resting orders: {e.order_count(front):,} | "
        f"cross-view consistency: {e.views_consistent(front) or '**OK**'}",
        "",
    ]

    bba = e.best_bid_ask(front)
    if bba:
        (bp, bs, bc), (ap, asz, ac) = bba
        lines += [
            f"**Best bid** {bp/1e9:.1f} ({bs} lots, {bc} orders)   "
            f"**Best ask** {ap/1e9:.1f} ({asz} lots, {ac} orders)   "
            f"**Spread** {e.spread_pts(front):.1f} pts",
            "",
        ]

    for side, name in (("B", "BID"), ("A", "ASK")):
        lv = e.top_levels(front, side, 10)
        ov = e.top_levels(front, side, 10, from_orders=True)
        lines += [f"## Top-10 {name} levels", ""]
        lines += fmt_table(lv, ov)
        lines += [""]

    lines += [
        "## Instruments in session (top 8 by traded volume)",
        "",
        "| symbol | instrument_id | records | T volume |",
        "|--------|---------------|---------|----------|",
    ]
    for iid, symb, n, vol in e.instruments()[:8]:
        lines.append(f"| {symb} | {iid} | {n:,} | {vol:,} |")
    lines += [
        "",
        "## Engine counters",
        "",
        "```",
        "\n".join(f"{k:28s} {v:,}" for k, v in sorted(s.items())),
        "```",
        "",
    ]

    out = cfg.storage.reports_dir / "milestone1_book.md"
    out.write_text("\n".join(lines))
    print("\n".join(lines[:40]))
    print(f"\nfull output -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
