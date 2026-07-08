"""Archive session/file resolution: date -> raw daily DBN file.

The raw archive is one .dbn.zst per UTC day under data/raw_mbo/daily/.
Empirically verified (2026-07-08): every daily file is self-contained — it
begins with per-instrument Clear ('R') records followed by snapshot Add
records (F_SNAPSHOT flag) that rebuild all resting orders, so single-file
replay reconstructs the full book without stitching previous days.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from utilities.config import Config, load_config

FILE_TEMPLATE = "glbx-mdp3-{ymd}.mbo.dbn.zst"


def session_file(date: dt.date, cfg: Config | None = None) -> Path:
    cfg = cfg or load_config()
    return cfg.data.raw_archive_dir / FILE_TEMPLATE.format(ymd=date.strftime("%Y%m%d"))


def list_session_dates(
    start: dt.date, end: dt.date, cfg: Config | None = None
) -> list[dt.date]:
    """All dates in [start, end] whose daily file exists on disk."""
    cfg = cfg or load_config()
    out: list[dt.date] = []
    d = start
    while d <= end:
        if session_file(d, cfg).exists():
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def dev_slice_files(cfg: Config | None = None) -> list[Path]:
    """Daily files of the configured ~2-week development slice (Step 2)."""
    cfg = cfg or load_config()
    dates = list_session_dates(cfg.data.dev_slice_start, cfg.data.dev_slice_end, cfg)
    return [session_file(d, cfg) for d in dates]
