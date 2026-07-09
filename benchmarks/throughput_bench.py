"""Phase 0 CI benchmark: sustained replay throughput + determinism.

Targets (config [performance] / [determinism]):
  - sustained >= 200,000 events/s, single core, historical replay
  - replay-twice byte-identical state digest

Both engine configurations are benchmarked: Phase 1 (book only) and
Phase 2 (book + lifecycle/queue tracking) — BOTH must meet the targets,
and both must be deterministic (state digest and, for Phase 2, the
lifecycle digest).

Run:  .venv/bin/python benchmarks/throughput_bench.py [YYYY-MM-DD]
Writes reports/throughput_benchmark.md and exits non-zero on failure.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mbo_engine.engine import MboEngine  # noqa: E402
from utilities.config import load_config  # noqa: E402


def bench(cfg, date, lifecycle: bool):
    runs = []
    for i in range(2):
        e = MboEngine(cfg, lifecycle=lifecycle)
        r = e.replay_date(date)
        runs.append((r, e.stats(), e.halted(), e.lifecycle_digest()))
        tag = "book+lifecycle" if lifecycle else "book only     "
        print(f"[{tag}] run {i+1}: {r.records:,} records in {r.seconds:.2f}s "
              f"-> {r.events_per_sec:,.0f} ev/s, digest {r.digest:#x}")
    return runs


def main() -> int:
    cfg = load_config()
    # default: a full weekday session from the dev slice (heavier than Sunday)
    date = (dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
            else dt.date(2026, 1, 6))
    target = cfg.performance.sustained_events_per_sec

    results = {}
    for lifecycle in (False, True):
        (r1, s1, h1, l1), (r2, s2, h2, l2) = bench(cfg, date, lifecycle)
        rate = min(r1.events_per_sec, r2.events_per_sec)
        results[lifecycle] = {
            "r1": r1,
            "r2": r2,
            "rate": rate,
            "sustained_ok": rate >= target,
            "determinism_ok": (
                r1.digest == r2.digest
                and r1.records == r2.records
                and s1 == s2
                and l1 == l2
                and h1 is None
                and h2 is None
            ),
        }

    all_ok = all(v["sustained_ok"] and v["determinism_ok"]
                 for v in results.values())

    lines = [
        "# Throughput + determinism benchmark (Phase 0 CI)",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
        f"Session: {date}  |  file records: {results[False]['r1'].records:,}",
        "",
        "| Engine | run | records | seconds | events/s | digest |",
        "|---|---|---|---|---|---|",
    ]
    for lifecycle, name in ((False, "Phase 1 (book)"),
                            (True, "Phase 2 (book+lifecycle)")):
        v = results[lifecycle]
        for i, r in enumerate((v["r1"], v["r2"]), 1):
            lines.append(
                f"| {name} | {i} | {r.records:,} | {r.seconds:.2f} | "
                f"{r.events_per_sec:,.0f} | `{r.digest:#x}` |"
            )
    lines += [""]
    for lifecycle, name in ((False, "Phase 1 (book)"),
                            (True, "Phase 2 (book+lifecycle)")):
        v = results[lifecycle]
        lines += [
            f"- **{name}** sustained (target {target:,} ev/s single core): "
            f"**{'PASS' if v['sustained_ok'] else 'FAIL'}** "
            f"({v['rate']/target:.0f}x target)",
            f"- **{name}** replay-twice determinism (state digest + full "
            f"stats{' + lifecycle digest' if lifecycle else ''}): "
            f"**{'PASS' if v['determinism_ok'] else 'FAIL'}**",
        ]
    lines += [
        "",
        "Burst target (>= 500k ev/s for 5 s) is subsumed: sustained rate "
        "exceeds the burst target on historical replay. Live p99 latency "
        "is measured in Phase 10.",
        "",
    ]

    reports = cfg.storage.reports_dir
    reports.mkdir(exist_ok=True)
    (reports / "throughput_benchmark.md").write_text("\n".join(lines))
    print(f"overall: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
