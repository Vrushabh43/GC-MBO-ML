"""Fetch the BLS release schedules (CPI, PPI, Employment Situation/NFP)
for 2017-2026 and write data/calendar/events_bls.csv (Step 12.5 / Phase
4.5 external calendar data — release TIMES only, never values).

Source: Wayback Machine snapshots of the official BLS per-release schedule
pages (bls.gov blocks direct fetching; the archived pages are the same
official tables). Each snapshot lists ~14 months (Oct of Y-1 .. Nov of Y);
one snapshot per year gives overlapping coverage and the union is deduped.
Every row carries the exact snapshot URL as provenance.

Times are America/New_York (BLS releases at 08:30 AM ET), converted to UTC
DST-aware. Tiers: CPI + Employment Situation = high, PPI = medium
(configurable by editing the CSV — the tier column is data, not code).

Also rebuilds the merged data/calendar/events.csv from all events_*.csv.

Run:  .venv/bin/python scripts/fetch_bls_calendar.py
"""
from __future__ import annotations

import csv
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")

RELEASES = {
    "cpi": ("CPI", "high"),
    "empsit": ("Employment Situation (NFP)", "high"),
    "ppi": ("PPI", "medium"),
}
YEARS = range(2017, 2027)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


CACHE = REPO / "data" / "calendar" / ".bls_cache"


def fetch(url: str, cache_key: str) -> str:
    """Polite cached fetch: Wayback throttles bursts (curl exit 7) and
    serves gzip (--compressed). Cached HTML makes reruns incremental."""
    import time

    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{cache_key}.html"
    if f.exists() and f.stat().st_size > 5000:
        return f.read_text(errors="ignore")
    time.sleep(3)  # pacing between live requests
    r = subprocess.run(
        ["curl", "-sL", "--compressed", "-A", "Mozilla/5.0",
         "--retry", "6", "--retry-delay", "8", "--retry-all-errors",
         "--max-time", "90", url],
        capture_output=True, text=True, check=True,
    )
    f.write_text(r.stdout)
    return r.stdout


def parse_rows(html: str) -> list[tuple[dt.date, dt.time]]:
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        date = time = None
        for c in cells:
            m = re.match(r"([A-Z][a-z]+)\.?\s+(\d{1,2}),\s+(\d{4})$", c)
            if m and m.group(1)[:3] in MONTHS:
                date = dt.date(int(m.group(3)), MONTHS[m.group(1)[:3]], int(m.group(2)))
            m = re.match(r"(\d{2}):(\d{2})\s+([AP])M$", c)
            if m:
                hh = int(m.group(1)) % 12 + (12 if m.group(3) == "P" else 0)
                time = dt.time(hh, int(m.group(2)))
        if date and time:
            out.append((date, time))
    return out


# coverage repair: extra snapshot timestamps where a yearly request
# redirected past part of a year (found by the gap check below)
SUPPLEMENTS: list[tuple[str, str]] = []
# the per-release PPI page has NO archived captures between 2017-06 and
# 2019-01 (verified via the CDX index); those months are backfilled from
# the archived BLS MONTHLY schedule calendars instead
MONTHLY_BACKFILL = [("ppi", "Producer Price Index", 2018, m) for m in range(1, 10)]


def parse_monthly(html: str, label: str, year: int, month: int) -> list[tuple[dt.date, dt.time]]:
    """BLS monthly schedule calendar grid: each cell starts with the day
    number and lists releases as '<name> <ref month> HH:MM AM'."""
    out = []
    for cell in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S):
        txt = re.sub(r"<[^>]+>", " ", cell)
        txt = re.sub(r"\s+", " ", txt).strip()
        m_day = re.match(r"^(\d{1,2})\b", txt)
        if not m_day:
            continue
        day = int(m_day.group(1))
        for m in re.finditer(
            rf"{label}\s+[A-Z][a-z]+ \d{{4}}\s+(\d{{2}}):(\d{{2}}) ([AP])M", txt
        ):
            hh = int(m.group(1)) % 12 + (12 if m.group(3) == "P" else 0)
            out.append((dt.date(year, month, day), dt.time(hh, int(m.group(2)))))
    return out


def main() -> int:
    events: dict[tuple[str, str], dict] = {}  # (name, ts) -> row
    failures = []
    jobs = [(rel, f"{y}0601") for rel in RELEASES for y in YEARS] + SUPPLEMENTS
    for rel, stamp in jobs:
        name, tier = RELEASES[rel]
        url = (f"https://web.archive.org/web/{stamp}id_/"
               f"https://www.bls.gov/schedule/news_release/{rel}.htm")
        try:
            rows = parse_rows(fetch(url, f"{rel}_{stamp}"))
        except Exception as e:  # noqa: BLE001
            failures.append((rel, stamp, str(e)))
            continue
        if len(rows) < 10:
            failures.append((rel, stamp, f"only {len(rows)} rows parsed"))
            continue
        for date, time in rows:
            t = dt.datetime.combine(date, time, tzinfo=ET)
            ts = t.astimezone(dt.timezone.utc).isoformat()
            events[(name, ts)] = {
                "ts_utc": ts, "name": name, "tier": tier, "source": url,
            }
        print(f"{rel} {stamp}: {len(rows)} rows")

    for rel, label, y, mo in MONTHLY_BACKFILL:
        name, tier = RELEASES[rel]
        url = (f"https://web.archive.org/web/{y}{mo:02d}20id_/"
               f"https://www.bls.gov/schedule/{y}/{mo:02d}_sched.htm")
        try:
            rows2 = parse_monthly(fetch(url, f"sched_{y}_{mo:02d}"), label, y, mo)
        except Exception as e:  # noqa: BLE001
            failures.append((rel, f"{y}-{mo:02d}", str(e)))
            continue
        if not rows2:
            failures.append((rel, f"{y}-{mo:02d}", "no rows in monthly page"))
            continue
        for date, time in rows2:
            t = dt.datetime.combine(date, time, tzinfo=ET)
            ts = t.astimezone(dt.timezone.utc).isoformat()
            events[(name, ts)] = {
                "ts_utc": ts, "name": name, "tier": tier, "source": url,
            }
        print(f"{rel} monthly {y}-{mo:02d}: {len(rows2)} rows")

    # coverage gate: no series may have a >45-day hole inside 2017-2026
    per_series: dict[str, list[str]] = {}
    for (name, ts) in sorted(events):
        per_series.setdefault(name, []).append(ts)
    for name, tss in per_series.items():
        keep = [t for t in tss if "2017" <= t[:4] <= "2026"]
        prev = None
        for t in keep:
            cur = dt.datetime.fromisoformat(t)
            if prev is not None and (cur - prev).days > 45:
                failures.append((name, "coverage", f"gap {prev.date()} .. {cur.date()}"))
            prev = cur

    rows = sorted(events.values(), key=lambda r: r["ts_utc"])
    rows = [r for r in rows if "2017" <= r["ts_utc"][:4] <= "2026"]
    cal_dir = REPO / "data" / "calendar"
    out = cal_dir / "events_bls.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts_utc", "name", "tier", "source"])
        w.writeheader()
        w.writerows(rows)
    by = {}
    for r in rows:
        by[r["name"]] = by.get(r["name"], 0) + 1
    print(f"{len(rows)} BLS events -> {out} | {by}")
    if failures:
        print("FAILURES (coverage gaps — do NOT ignore):")
        for f_ in failures:
            print(" ", f_)

    # rebuild the merged calendar from every per-source file
    merged: list[dict] = []
    for src in sorted(cal_dir.glob("events_*.csv")):
        with open(src, newline="") as f:
            merged.extend(csv.DictReader(f))
    merged.sort(key=lambda r: r["ts_utc"])
    with open(cal_dir / "events.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts_utc", "name", "tier", "source"])
        w.writeheader()
        w.writerows(merged)
    print(f"{len(merged)} total events -> {cal_dir / 'events.csv'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
