"""Scheduled-release event calendar (plan Phase 4.5, Step 12.5).

The calendar is an EXTERNAL, VERSIONED dataset: CSV files under
data/calendar/ (schema in data/calendar/README.md; columns ts_utc, name,
tier, source). This module never fabricates release dates — an empty or
missing calendar yields well-defined "no scheduled event" values and a
loud `coverage_warning`.

Leakage note (past-only rule): release TIMES are published well in advance
— using the schedule at time t is calendar knowledge, not future leakage.
Realized release VALUES are never ingested anywhere in this system.

Features (plan): seconds_to_next_scheduled_event,
seconds_since_last_scheduled_event, event-tier encodings; plus the live
blackout (default 2 min before to 5 min after HIGH-impact) and the
label-hygiene decision for Phase 7 (high -> exclude, medium -> tag;
configurable).
"""
from __future__ import annotations

import bisect
import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

from utilities.config import Config

TIER_CODE = {"none": 0, "low": 1, "medium": 2, "high": 3}
NS = 1_000_000_000


@dataclass(frozen=True)
class ScheduledEvent:
    ts_ns: int
    name: str
    tier: str  # high | medium | low
    source: str


@dataclass
class EventCalendar:
    events: list[ScheduledEvent] = field(default_factory=list)  # ts-sorted
    blackout_pre_ns: int = 120 * NS
    blackout_post_ns: int = 300 * NS
    label_policy_high: str = "exclude"
    label_policy_medium: str = "tag"
    source_files: list[str] = field(default_factory=list)

    # -- construction --------------------------------------------------------

    @classmethod
    def load(cls, cfg: Config) -> "EventCalendar":
        c = cfg.raw["calendar"]
        path = Path(c["events_file"])
        if not path.is_absolute():
            from utilities.config import REPO_ROOT

            path = REPO_ROOT / path
        cal = cls(
            blackout_pre_ns=int(c["blackout_pre_s"]) * NS,
            blackout_post_ns=int(c["blackout_post_s"]) * NS,
            label_policy_high=c["label_policy_high"],
            label_policy_medium=c["label_policy_medium"],
        )
        if path.exists():
            cal.ingest_csv(path)
        return cal

    def ingest_csv(self, path: Path) -> int:
        """Add events from one CSV (schema: ts_utc,name,tier,source)."""
        import datetime as dt

        n = 0
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                t = dt.datetime.fromisoformat(row["ts_utc"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt.timezone.utc)
                tier = row["tier"].strip().lower()
                if tier not in ("high", "medium", "low"):
                    raise ValueError(f"{path}: bad tier {tier!r}")
                self.events.append(
                    ScheduledEvent(
                        ts_ns=int(t.timestamp() * NS),
                        name=row["name"].strip(),
                        tier=tier,
                        source=row.get("source", "").strip(),
                    )
                )
                n += 1
        self.events.sort(key=lambda e: e.ts_ns)
        self.source_files.append(str(path))
        return n

    def _keys(self) -> list[int]:
        return [e.ts_ns for e in self.events]

    def coverage_warning(self, t0_ns: int, t1_ns: int) -> str | None:
        """Loud signal for label hygiene: None only if the calendar has any
        event inside [t0, t1] padded by 45 days each side — otherwise the
        period is (partially) uncovered and exclusion/tagging is blind."""
        pad = 45 * 24 * 3600 * NS
        i = bisect.bisect_left(self._keys(), t0_ns - pad)
        if i < len(self.events) and self.events[i].ts_ns <= t1_ns + pad:
            return None
        return (
            f"calendar has no events within 45 days of "
            f"[{t0_ns}, {t1_ns}] — release data not ingested for this period"
        )

    # -- features (plan Phase 4.5) -------------------------------------------

    def next_event(self, ts_ns: int) -> ScheduledEvent | None:
        i = bisect.bisect_right(self._keys(), ts_ns)
        return self.events[i] if i < len(self.events) else None

    def last_event(self, ts_ns: int) -> ScheduledEvent | None:
        i = bisect.bisect_right(self._keys(), ts_ns)
        return self.events[i - 1] if i > 0 else None

    def features(self, ts_ns: int) -> dict[str, float]:
        """seconds_to_next / seconds_since_last / tier codes. With no
        event on a side: seconds are +inf (Phase 5 standardization caps
        via log1p+winsorize), tier code 0 (= none)."""
        nxt, lst = self.next_event(ts_ns), self.last_event(ts_ns)
        return {
            "seconds_to_next_scheduled_event": (
                (nxt.ts_ns - ts_ns) / NS if nxt else math.inf
            ),
            "seconds_since_last_scheduled_event": (
                (ts_ns - lst.ts_ns) / NS if lst else math.inf
            ),
            "next_event_tier": float(TIER_CODE[nxt.tier if nxt else "none"]),
            "last_event_tier": float(TIER_CODE[lst.tier if lst else "none"]),
        }

    # -- live blackout + label hygiene ----------------------------------------

    def in_blackout(self, ts_ns: int) -> bool:
        """Inside [pre, post] of any HIGH-impact event (live suppression)."""
        keys = self._keys()
        i = bisect.bisect_right(keys, ts_ns + self.blackout_pre_ns)
        # any high event e with e.ts - pre <= ts <= e.ts + post
        for e in self.events[max(0, i - 8) : i]:
            if e.tier == "high" and ts_ns <= e.ts_ns + self.blackout_post_ns:
                return True
        return False

    def label_window_policy(self, t0_ns: int, t1_ns: int) -> str:
        """Phase 7 hygiene for a label window [t0, t1]:
        'exclude' | 'tag' | 'ok' (high beats medium; config-driven)."""
        keys = self._keys()
        lo = bisect.bisect_left(keys, t0_ns)
        hi = bisect.bisect_right(keys, t1_ns)
        decision = "ok"
        for e in self.events[lo:hi]:
            if e.tier == "high":
                return self.label_policy_high
            if e.tier == "medium":
                decision = self.label_policy_medium
        return decision
