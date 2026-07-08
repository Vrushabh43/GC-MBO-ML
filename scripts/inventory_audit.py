"""Step 2 — full-archive inventory audit (Build Order Step 2, plan v2.4/v2.5).

Checks, per gc_orderflow_plan_v2.md:
  1. File completeness per session (calendar scan vs non-Saturday days).
  2. Gap scan (missing days; cross-checked against KNOWN_GAPS.md expectations).
  3. DBN schema/version consistency across all years (reader must handle all).
  4. Size/compression report.
  5. R10 quality mask seed: per-date status from vendor condition.json reports
     (available / degraded / missing), plus the quarantined 2020-02-28 hole.
  6. Decode spot-check on sampled files spread across every year.
  7. Development-slice verification (config [data.dev_slice]): all days
     present, none degraded.

Outputs (versioned, under reports/):
  reports/archive_audit.md            — human-readable audit report
  reports/archive_audit_files.parquet — per-file table (date, size, dbn_version, ...)
  reports/quality_mask.csv            — per-date R10 quality mask seed

No feature work until this report exists (plan Phase 0 data section).
"""
from __future__ import annotations

import collections
import datetime as dt
import glob
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from utilities.config import REPO_ROOT, load_config  # noqa: E402

import databento as db  # noqa: E402

CFG = load_config()
RAW = CFG.data.raw_archive_dir
REPORTS = CFG.storage.reports_dir
JOB_META = REPO_ROOT / "data" / "raw_mbo" / "job_metadata"

SPOT_DECODE_PER_YEAR = 3          # files fully-opened + first/last records read
SPOT_DECODE_RECORDS = 50_000      # records decoded per sampled file


def scan_files() -> pd.DataFrame:
    rows = []
    files = sorted(RAW.glob("glbx-mdp3-*.mbo.dbn.zst"))
    t0 = time.time()
    for i, p in enumerate(files):
        name = p.name
        date = dt.date.fromisoformat(f"{name[10:14]}-{name[14:16]}-{name[16:18]}")
        size = p.stat().st_size
        try:
            store = db.DBNStore.from_file(p)
            m = store.metadata
            rows.append(
                dict(date=date, file=name, size_bytes=size,
                     dbn_version=m.version, dataset=m.dataset,
                     schema=str(m.schema), ts_start=m.start, ts_end=m.end,
                     metadata_ok=True, error=""))
        except Exception as e:  # metadata unreadable = hard failure
            rows.append(
                dict(date=date, file=name, size_bytes=size,
                     dbn_version=-1, dataset="", schema="",
                     ts_start=0, ts_end=0, metadata_ok=False, error=str(e)))
        if (i + 1) % 250 == 0:
            print(f"  metadata scan {i+1}/{len(files)} ({time.time()-t0:.0f}s)", flush=True)
    return pd.DataFrame(rows)


def calendar_gaps(df: pd.DataFrame) -> list[dt.date]:
    have = set(df.date)
    missing, d = [], CFG.data.archive_start
    while d <= CFG.data.archive_end:
        if d not in have and d.strftime("%A") != "Saturday":
            missing.append(d)
        d += dt.timedelta(days=1)
    return missing


def vendor_conditions() -> dict[dt.date, str]:
    """Merge condition.json from every batch-job dir; worst condition wins."""
    rank = {"available": 0, "pending": 1, "degraded": 2, "missing": 3}
    out: dict[dt.date, str] = {}
    for cj in sorted(glob.glob(str(JOB_META / "GLBX-*" / "condition.json"))):
        with open(cj) as f:
            for e in json.load(f):
                d = dt.date.fromisoformat(e["date"])
                c = e.get("condition", "available")
                if rank.get(c, 0) >= rank.get(out.get(d, "available"), 0):
                    out[d] = c
    return out


def spot_decode(df: pd.DataFrame) -> list[dict]:
    results = []
    ok = df[df.metadata_ok]
    for year, grp in ok.groupby(ok.date.map(lambda d: d.year)):
        # first, middle, last file of each year
        idxs = sorted({0, len(grp) // 2, len(grp) - 1})
        for j in idxs[:SPOT_DECODE_PER_YEAR]:
            p = RAW / grp.iloc[j]["file"]
            r0 = None
            n = 0
            t0 = time.time()
            try:
                store = db.DBNStore.from_file(p)
                for rec in store:
                    if n == 0:
                        r0 = (chr(rec.action) if isinstance(rec.action, int)
                              else str(rec.action))
                    n += 1
                    if n >= SPOT_DECODE_RECORDS:
                        break
                results.append(dict(file=p.name, year=int(year), decoded=n,
                                    first_action=str(r0),
                                    secs=round(time.time() - t0, 2), ok=True, error=""))
            except Exception as e:
                results.append(dict(file=p.name, year=int(year), decoded=n,
                                    first_action="", secs=0.0, ok=False, error=str(e)))
            print(f"  spot-decode {p.name}: {results[-1]}", flush=True)
    return results


def build_quality_mask(df: pd.DataFrame, missing: list[dt.date],
                       cond: dict[dt.date, str]) -> pd.DataFrame:
    rows = []
    d = CFG.data.archive_start
    have = set(df.date)
    while d <= CFG.data.archive_end:
        if d.strftime("%A") == "Saturday":
            d += dt.timedelta(days=1)
            continue
        if d not in have:
            status, reason = "missing", (
                "unrecoverable vendor hole (KNOWN_GAPS.md)"
                if d in CFG.data.known_missing_days else "absent from archive")
        elif cond.get(d, "available") == "degraded":
            status, reason = "degraded", "vendor condition.json = degraded"
        else:
            status, reason = "ok", ""
        rows.append(dict(date=d, status=status, reason=reason))
        d += dt.timedelta(days=1)
    return pd.DataFrame(rows)


def main() -> int:
    REPORTS.mkdir(exist_ok=True)
    print("Step 2 inventory audit starting.", flush=True)

    df = scan_files()
    missing = calendar_gaps(df)
    cond = vendor_conditions()
    mask = build_quality_mask(df, missing, cond)
    decode = spot_decode(df)

    # dev-slice verification
    slice_days = mask[(mask.date >= CFG.data.dev_slice_start)
                      & (mask.date <= CFG.data.dev_slice_end)]
    slice_ok = bool((slice_days.status == "ok").all()) and len(slice_days) > 0

    version_hist = df.groupby([df.date.map(lambda d: d.year), "dbn_version"]).size()
    total_gb = df.size_bytes.sum() / 1e9
    problems = []
    if not df.metadata_ok.all():
        problems.append(f"{(~df.metadata_ok).sum()} files with unreadable metadata")
    unexpected_missing = [m for m in missing if m not in CFG.data.known_missing_days]
    if unexpected_missing:
        problems.append(f"UNEXPECTED missing days: {unexpected_missing}")
    if not all(r["ok"] for r in decode):
        problems.append("spot-decode failures: "
                        + str([r["file"] for r in decode if not r["ok"]]))
    if not slice_ok:
        problems.append("dev slice has missing/degraded days")

    df.to_parquet(REPORTS / "archive_audit_files.parquet", index=False)
    mask.to_csv(REPORTS / "quality_mask.csv", index=False)

    lines = [
        "# Archive inventory audit (Build Order Step 2)",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
        f"Archive: `{RAW}`  |  spec: gc_orderflow_plan_v2.md v2.5",
        "",
        "## Summary",
        "",
        f"- Files: **{len(df)}**  ({df.date.min()} .. {df.date.max()})",
        f"- Total compressed size: **{total_gb:.1f} GB**"
        f"  (min {df.size_bytes.min()/1e6:.2f} MB, median {df.size_bytes.median()/1e6:.1f} MB,"
        f" max {df.size_bytes.max()/1e6:.1f} MB)",
        f"- Metadata readable: **{int(df.metadata_ok.sum())}/{len(df)}**",
        f"- DBN versions present: **{sorted(df[df.metadata_ok].dbn_version.unique().tolist())}**",
        f"- Dataset/schema uniform: "
        f"**{df[df.metadata_ok].dataset.nunique() == 1 and df[df.metadata_ok].schema.nunique() == 1}**",
        f"- Missing non-Saturday days: **{len(missing)}** -> {[str(m) for m in missing]}",
        f"- Degraded days (vendor): **{int((mask.status == 'degraded').sum())}**",
        f"- Quality mask rows: {len(mask)} (reports/quality_mask.csv)",
        "",
        "## DBN version histogram (year x version)",
        "",
        "```",
        str(version_hist),
        "```",
        "",
        "## Decode spot-check "
        f"({len(decode)} files, {SPOT_DECODE_RECORDS} records each)",
        "",
        "```",
        "\n".join(f"{r['file']}: ok={r['ok']} decoded={r['decoded']} "
                  f"({r['secs']}s) {r['error']}" for r in decode),
        "```",
        "",
        "## Development slice (Step 2 selection)",
        "",
        f"- Range: {CFG.data.dev_slice_start} .. {CFG.data.dev_slice_end}",
        f"- Sessions present: {len(slice_days)}, all status=ok: **{slice_ok}**",
        "",
        "## Problems",
        "",
        ("NONE — archive passes the audit." if not problems
         else "\n".join(f"- **{p}**" for p in problems)),
        "",
    ]
    (REPORTS / "archive_audit.md").write_text("\n".join(lines))
    print("\n".join(lines[-12:]))
    print(f"\nWrote {REPORTS}/archive_audit.md, archive_audit_files.parquet, quality_mask.csv")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
