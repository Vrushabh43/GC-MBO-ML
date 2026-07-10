"""Generate the FOMC scheduled-release events CSV (Step 12.5 seed data).

Provenance: meeting dates fetched 2026-07-10 from federalreserve.gov —
  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (2021-2026)
  https://www.federalreserve.gov/monetarypolicy/fomchistorical{2017,2018,2019,2020}.htm
Event instant: the policy STATEMENT release, 14:00 America/New_York on the
final meeting day (the Fed's standard since 2013; DST-aware conversion).

Deliberately EXCLUDED (documented, not fabricated):
  - 2019-10-04 unscheduled conference call
  - 2020 March emergency actions (Mar 3 / Mar 15 announcements), the
    cancelled Mar 17-18 meeting, and 2020/2025 notation votes — their
    announcement instants were irregular; ingest them from a curated
    source if that period's label hygiene ever needs finer coverage
    (2020 spring is the tagged COVID stratum regardless).
  - FOMC minutes (medium tier, ~3 weeks later) — dates not yet sourced.

Writes data/calendar/events_fomc.csv and merges all per-source files into
data/calendar/events.csv (the config [calendar] events_file).
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
SRC = "federalreserve.gov fomccalendars/fomchistorical (fetched 2026-07-10); statement 14:00 ET final day"

# final day of each regularly scheduled meeting
FINAL_DAYS = {
    2017: ["02-01", "03-15", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2018: ["01-31", "03-21", "05-02", "06-13", "08-01", "09-26", "11-08", "12-19"],
    2019: ["01-30", "03-20", "05-01", "06-19", "07-31", "09-18", "10-30", "12-11"],
    2020: ["01-29", "04-29", "06-10", "07-29", "09-16", "11-05", "12-16"],
    2021: ["01-27", "03-17", "04-28", "06-16", "07-28", "09-22", "11-03", "12-15"],
    2022: ["01-26", "03-16", "05-04", "06-15", "07-27", "09-21", "11-02", "12-14"],
    2023: ["02-01", "03-22", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2024: ["01-31", "03-20", "05-01", "06-12", "07-31", "09-18", "11-07", "12-18"],
    2025: ["01-29", "03-19", "05-07", "06-18", "07-30", "09-17", "10-29", "12-10"],
    2026: ["01-28", "03-18", "04-29", "06-17", "07-29", "09-16", "10-28", "12-09"],
}


def main() -> int:
    cal_dir = REPO / "data" / "calendar"
    cal_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for year, days in sorted(FINAL_DAYS.items()):
        for md in days:
            d = dt.date.fromisoformat(f"{year}-{md}")
            t = dt.datetime(d.year, d.month, d.day, 14, 0, tzinfo=ET)
            rows.append(
                {
                    "ts_utc": t.astimezone(dt.timezone.utc).isoformat(),
                    "name": "FOMC statement",
                    "tier": "high",
                    "source": SRC,
                }
            )

    fomc = cal_dir / "events_fomc.csv"
    with open(fomc, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts_utc", "name", "tier", "source"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} FOMC statement events -> {fomc}")

    # merge every per-source file into the configured events.csv
    merged: list[dict] = []
    for src in sorted(cal_dir.glob("events_*.csv")):
        with open(src, newline="") as f:
            merged.extend(csv.DictReader(f))
    merged.sort(key=lambda r: r["ts_utc"])
    out = cal_dir / "events.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts_utc", "name", "tier", "source"])
        w.writeheader()
        w.writerows(merged)
    print(f"{len(merged)} total events -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
