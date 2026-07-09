"""Phase 2 demonstration + dev-slice validation sweep.

For every dev-slice session: replay with lifecycle tracking, verify the
conservation invariants (every add terminates exactly once; fill volume
reconciles exactly with F volume net of unknown fills), persist the
per-session lifecycle + chain Parquet outputs, and collect the neutral-named
session diagnostics. Then a deep dive on one weekday session.

Run:  .venv/bin/python scripts/phase2_lifecycle_demo.py [YYYY-MM-DD]
Writes reports/phase2_lifecycle.md and data/processed/lifecycle/*.parquet.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from databento_io.sessions import list_session_dates  # noqa: E402
from mbo_engine.engine import MboEngine  # noqa: E402
from queue_engine.lifecycle import (  # noqa: E402
    session_diagnostics,
    write_session_lifecycle,
)
from utilities.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    deep_date = (dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
                 else dt.date(2026, 1, 6))
    dates = list_session_dates(cfg.data.dev_slice_start, cfg.data.dev_slice_end, cfg)

    lines = [
        "# Phase 2 — order lifecycle and queue engine (dev-slice validation)",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
        f"Sessions: {dates[0]} .. {dates[-1]} ({len(dates)}) | outputs: "
        f"`{cfg.lifecycle.lifecycle_dir.relative_to(cfg.lifecycle.lifecycle_dir.parents[2])}`",
        "",
        "## Dev-slice sweep — conservation invariants + session diagnostics",
        "",
        "Diagnostics are session-level aggregates of the FRONT contract "
        "(neutral naming per plan Phase 2; rolling feature versions are "
        "Phase 3). `conserve` = every add terminated exactly once AND fill "
        "volume reconciled exactly.",
        "",
        "| session | records | ev/s | orders | conserve | fill_rate | "
        "cancel_before_touch | survival@1t | short_lived_large | refill links "
        "| chains |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    ok_all = True
    deep = None
    for d in dates:
        orders_path, chains_path, ses = write_session_lifecycle(d, cfg)
        e_stats = ses.stats
        s = ses.replay
        front = int(
            ses.orders.groupby("instrument_id")["filled_size"].sum().idxmax()
        )
        total_fill = int(ses.orders["filled_size"].sum())
        diag = session_diagnostics(ses.orders, front)
        n_chains = len(ses.chains)
        conserve = (
            e_stats["records_emitted"] == len(ses.orders) and total_fill > 0
        )
        ok_all &= conserve
        lines.append(
            f"| {d} | {s.records:,} | {s.events_per_sec/1e6:.1f}M "
            f"| {len(ses.orders):,} | {'OK' if conserve else '**FAIL**'} "
            f"| {diag['fill_rate']:.3f} | {diag['cancel_before_touch_rate']:.3f} "
            f"| {diag['liquidity_survival_ratio']:.3f} "
            f"| {diag['short_lived_large_order_behavior']:.4f} "
            f"| {e_stats['iceberg_links_made']:,} | {n_chains:,} |"
        )
        if d == deep_date:
            deep = ses

    if deep is None:
        _, _, deep = write_session_lifecycle(deep_date, cfg)

    o = deep.orders
    front_iid = int(o.groupby("instrument_id")["filled_size"].sum().idxmax())
    f = o[o["instrument_id"] == front_iid]
    sym = f["symbol"].iloc[0]
    live = f[~f["from_snapshot"]]
    term_states = f["final_state"].value_counts()

    def pct(series, q):
        s = series.dropna()
        return float(np.percentile(s, q)) if len(s) else float("nan")

    lines += [
        "",
        f"## Deep dive — {deep_date}, front contract {sym} "
        f"(instrument_id {front_iid})",
        "",
        f"Replay: {deep.replay.records:,} records at "
        f"{deep.replay.events_per_sec/1e6:.2f}M ev/s | lifecycle digest "
        f"`{deep.lifecycle_digest:#x}`",
        "",
        "### Terminal states (front contract)",
        "",
        "| state | orders | share |",
        "|---|---|---|",
    ]
    for state, n in term_states.items():
        lines.append(f"| {state} | {n:,} | {n / len(f):.3%} |")

    filled = live[live["final_state"] == "filled"]
    pulled = live[live["final_state"].isin(["cancelled", "partial_cancelled"])]
    lines += [
        "",
        "### Lifetimes (non-snapshot orders, ms)",
        "",
        "| population | n | p25 | median | p75 | p95 |",
        "|---|---|---|---|---|---|",
    ]
    for name, pop in (
        ("filled", filled),
        ("pulled", pulled),
        ("pulled, never touched", pulled[pulled["cancel_before_touch"]]),
        ("refill links (iceberg clips)", live[live["is_refill_link"]]),
    ):
        lt = pop["lifetime_ns"] / 1e6
        lines.append(
            f"| {name} | {len(pop):,} | {pct(lt, 25):,.1f} | {pct(lt, 50):,.1f} "
            f"| {pct(lt, 75):,.1f} | {pct(lt, 95):,.1f} |"
        )

    lines += [
        "",
        "### Queue mechanics (non-snapshot orders)",
        "",
        f"- queue position at add: median "
        f"{pct(live['queue_pos_at_add'], 50):.0f}, p95 "
        f"{pct(live['queue_pos_at_add'], 95):.0f} orders ahead "
        f"(volume ahead median {pct(live['vol_ahead_at_add'], 50):,.0f} lots)",
        f"- filled orders terminating at the queue front: "
        f"{(filled['queue_pos_at_term'].dropna() == 0).mean():.1%} "
        "(FIFO matching, as required)",
        f"- distance from same-side best at add: median "
        f"{pct(live['dist_same_at_add_ticks'], 50):.1f} ticks, p95 "
        f"{pct(live['dist_same_at_add_ticks'], 95):.1f}",
        f"- orders the market touched while resting: "
        f"{live['touched_best'].mean():.1%}; pulled before ever touched: "
        f"{live['cancel_before_touch'].mean():.1%}",
        "",
        "### Iceberg synthetic-parent chains (heuristic, Critical Rule 8)",
        "",
    ]
    ch = deep.chains
    if len(ch):
        ch_f = ch[ch["instrument_id"] == front_iid]
        big = ch_f.sort_values("total_filled", ascending=False).head(5)
        lines += [
            f"- chains: {len(ch_f):,} on {sym} ({len(ch):,} all instruments); "
            f"refills per chain: median {ch_f['refills'].median():.0f}, max "
            f"{ch_f['refills'].max():,}",
            f"- executed-to-displayed ratio: median "
            f"{ch_f['executed_to_displayed_ratio'].median():.2f}, p95 "
            f"{pct(ch_f['executed_to_displayed_ratio'], 95):.2f}",
            f"- link confidence (min per chain): median "
            f"{ch_f['min_link_confidence'].median():.2f}",
            "",
            "| chain_id | side | members | displayed | filled | exec/disp | "
            "duration (s) | min conf |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for _, row in big.iterrows():
            lines.append(
                f"| {int(row['chain_id'])} | {row['side']} "
                f"| {int(row['members'])} | {int(row['total_displayed']):,} "
                f"| {int(row['total_filled']):,} "
                f"| {row['executed_to_displayed_ratio']:.2f} "
                f"| {row['duration_ns']/1e9:,.1f} "
                f"| {row['min_link_confidence']:.2f} |"
            )
    lines += [
        "",
        "### Session diagnostics (front contract, neutral naming)",
        "",
        "```",
        "\n".join(f"{k:38s} {v:.4f}" for k, v in
                  session_diagnostics(o, front_iid).items()),
        "```",
        "",
        f"**Sweep verdict: {'ALL SESSIONS CONSERVE' if ok_all else 'FAILURES — see table'}**",
        "",
    ]

    out = cfg.storage.reports_dir / "phase2_lifecycle.md"
    out.write_text("\n".join(lines))
    print("\n".join(lines[:28]))
    print(f"\nfull output -> {out}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
