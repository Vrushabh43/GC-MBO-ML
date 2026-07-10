"""Session-phase classification (plan Phase 4.5, Step 12.5).

Pure, stateless functions of the event timestamp — the CME/COMEX trading
day is defined on the America/New_York clock, so phases are DST-aware via
zoneinfo (a 23:00 vs 22:00 UTC Globex open is the SAME 18:00 ET open —
matches the Phase 1 empirical finding).

Phases (Globex metals, boundaries from config [calendar]):
    maintenance   17:00-18:00 ET  (daily halt)
    asia          18:00-03:00 ET  (overnight/Asia)
    london        03:00-08:20 ET
    ny            08:20-13:30 ET  (COMEX floor hours)
    post          13:30-17:00 ET  (post-settlement)
plus an `is_settlement` flag for the COMEX gold settlement window
(13:29-13:30 ET by default), which lies inside `ny`.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from utilities.config import Config

PHASES = ("maintenance", "asia", "london", "ny", "post")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass(frozen=True)
class PhaseClock:
    """Config-driven phase boundaries on the exchange clock."""

    tz: ZoneInfo
    maintenance_start: int
    asia_start: int
    london_start: int
    ny_start: int
    post_start: int
    settle_start: int
    settle_end: int

    @classmethod
    def from_config(cls, cfg: Config) -> "PhaseClock":
        c = cfg.raw["calendar"]
        b = c["phase_boundaries"]
        s = c["settlement_window"]
        return cls(
            tz=ZoneInfo(c["tz"]),
            maintenance_start=_minutes(b["maintenance_start"]),
            asia_start=_minutes(b["asia_start"]),
            london_start=_minutes(b["london_start"]),
            ny_start=_minutes(b["ny_start"]),
            post_start=_minutes(b["post_start"]),
            settle_start=_minutes(s["start"]),
            settle_end=_minutes(s["end"]),
        )

    def local_minute(self, ts_ns: int) -> int:
        """Minutes since midnight on the exchange clock (DST-aware)."""
        t = dt.datetime.fromtimestamp(ts_ns / 1e9, tz=dt.timezone.utc)
        loc = t.astimezone(self.tz)
        return loc.hour * 60 + loc.minute

    def phase(self, ts_ns: int) -> str:
        m = self.local_minute(ts_ns)
        if self.london_start <= m < self.ny_start:
            return "london"
        if self.ny_start <= m < self.post_start:
            return "ny"
        if self.post_start <= m < self.maintenance_start:
            return "post"
        if self.maintenance_start <= m < self.asia_start:
            return "maintenance"
        return "asia"  # 18:00 ET through 03:00 ET, wrapping midnight

    def is_settlement(self, ts_ns: int) -> bool:
        m = self.local_minute(ts_ns)
        return self.settle_start <= m < self.settle_end

    def classify(self, ts_ns: int) -> tuple[str, bool]:
        return self.phase(ts_ns), self.is_settlement(ts_ns)
