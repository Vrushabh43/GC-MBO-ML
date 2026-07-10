"""Step 12.5 tests — session phases, scheduled-event features, blackout,
and label hygiene (plan Phase 4.5), plus the roll-ledger symbol/expiry
helpers. Event data uses synthetic fixtures — the module must never invent
real release dates.
"""
import datetime as dt
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calendar_mod.events import EventCalendar, ScheduledEvent  # noqa: E402
from calendar_mod.session_phase import PhaseClock  # noqa: E402
from utilities.config import load_config  # noqa: E402

CFG = load_config()
ET = ZoneInfo("America/New_York")
NS = 1_000_000_000


def ts_et(y, m, d, hh, mm, ss=0) -> int:
    t = dt.datetime(y, m, d, hh, mm, ss, tzinfo=ET)
    return int(t.timestamp() * NS)


def ts_utc(y, m, d, hh, mm, ss=0) -> int:
    t = dt.datetime(y, m, d, hh, mm, ss, tzinfo=dt.timezone.utc)
    return int(t.timestamp() * NS)


class TestSessionPhase:
    clock = PhaseClock.from_config(CFG)

    @pytest.mark.parametrize(
        "hh,mm,phase",
        [
            (17, 30, "maintenance"),
            (18, 0, "asia"),
            (23, 45, "asia"),
            (2, 59, "asia"),
            (3, 0, "london"),
            (8, 19, "london"),
            (8, 20, "ny"),
            (13, 29, "ny"),
            (13, 30, "post"),
            (16, 59, "post"),
        ],
    )
    def test_phase_boundaries(self, hh, mm, phase):
        assert self.clock.phase(ts_et(2024, 1, 16, hh, mm)) == phase

    def test_settlement_window_flag(self):
        assert self.clock.is_settlement(ts_et(2024, 1, 16, 13, 29, 30))
        assert not self.clock.is_settlement(ts_et(2024, 1, 16, 13, 30, 0))
        assert not self.clock.is_settlement(ts_et(2024, 1, 16, 13, 28, 59))

    def test_dst_awareness_same_local_phase(self):
        """18:30 ET is 'asia' whether that is 23:30 UTC (winter) or
        22:30 UTC (summer) — matches the Phase 1 empirical 23:00/22:00 UTC
        session-open finding."""
        winter = ts_utc(2024, 1, 16, 23, 30)
        summer = ts_utc(2024, 7, 16, 22, 30)
        assert self.clock.phase(winter) == "asia"
        assert self.clock.phase(summer) == "asia"
        # and the same UTC wall time lands in DIFFERENT phases across DST
        assert self.clock.phase(ts_utc(2024, 1, 16, 22, 30)) == "maintenance"


@pytest.fixture()
def calendar(tmp_path) -> EventCalendar:
    csv = tmp_path / "events.csv"
    csv.write_text(
        "ts_utc,name,tier,source\n"
        "2024-03-12T12:30:00+00:00,CPI,high,fixture\n"
        "2024-03-12T18:00:00+00:00,10y auction,medium,fixture\n"
        "2024-03-13T14:00:00+00:00,minor,low,fixture\n"
    )
    cal = EventCalendar(
        blackout_pre_ns=120 * NS,
        blackout_post_ns=300 * NS,
        label_policy_high="exclude",
        label_policy_medium="tag",
    )
    assert cal.ingest_csv(csv) == 3
    return cal


class TestEventFeatures:
    def test_seconds_to_next_and_since_last(self, calendar):
        t = ts_utc(2024, 3, 12, 12, 0)
        f = calendar.features(t)
        assert f["seconds_to_next_scheduled_event"] == 1800.0
        assert f["next_event_tier"] == 3.0  # high
        assert math.isinf(f["seconds_since_last_scheduled_event"])
        assert f["last_event_tier"] == 0.0  # none

        t2 = ts_utc(2024, 3, 12, 13, 0)
        f2 = calendar.features(t2)
        assert f2["seconds_since_last_scheduled_event"] == 1800.0
        assert f2["last_event_tier"] == 3.0
        assert f2["seconds_to_next_scheduled_event"] == pytest.approx(5 * 3600)
        assert f2["next_event_tier"] == 2.0  # medium

    def test_schedule_is_past_only_knowledge(self, calendar):
        """The features at time t depend only on the schedule, which is
        published in advance — adding a FUTURE event changes the forward
        view but never the backward one."""
        t = ts_utc(2024, 3, 12, 13, 0)
        before = calendar.features(t)["seconds_since_last_scheduled_event"]
        calendar.events.append(
            ScheduledEvent(ts_utc(2024, 3, 20, 12, 0), "later", "high", "fixture")
        )
        calendar.events.sort(key=lambda e: e.ts_ns)
        assert calendar.features(t)["seconds_since_last_scheduled_event"] == before

    def test_blackout_high_only(self, calendar):
        rel = ts_utc(2024, 3, 12, 12, 30)
        assert calendar.in_blackout(rel - 119 * NS)
        assert calendar.in_blackout(rel + 299 * NS)
        assert not calendar.in_blackout(rel - 121 * NS)
        assert not calendar.in_blackout(rel + 301 * NS)
        med = ts_utc(2024, 3, 12, 18, 0)
        assert not calendar.in_blackout(med)  # medium never blacks out

    def test_label_window_policy(self, calendar):
        rel = ts_utc(2024, 3, 12, 12, 30)
        assert calendar.label_window_policy(rel - 60 * NS, rel + 60 * NS) == "exclude"
        med = ts_utc(2024, 3, 12, 18, 0)
        assert calendar.label_window_policy(med - 60 * NS, med + 60 * NS) == "tag"
        low = ts_utc(2024, 3, 13, 14, 0)
        assert calendar.label_window_policy(low - 60 * NS, low + 60 * NS) == "ok"
        quiet = ts_utc(2024, 3, 12, 9, 0)
        assert calendar.label_window_policy(quiet, quiet + 60 * NS) == "ok"

    def test_coverage_warning_is_loud(self, calendar):
        inside = ts_utc(2024, 3, 12, 0, 0)
        assert calendar.coverage_warning(inside, inside + 60 * NS) is None
        far = ts_utc(2020, 6, 1, 0, 0)
        assert calendar.coverage_warning(far, far + 60 * NS) is not None

    def test_empty_calendar_never_silently_ok(self):
        cal = EventCalendar()
        t = ts_utc(2024, 3, 12, 12, 0)
        assert cal.coverage_warning(t, t) is not None
        f = cal.features(t)
        assert math.isinf(f["seconds_to_next_scheduled_event"])
        assert f["next_event_tier"] == 0.0


class TestRollLedgerAccess:
    """Against the BUILT full-archive ledger (data/calendar/)."""

    def test_active_and_roll_boundaries(self):
        from calendar_mod.roll_ledger import RollLedger

        led = RollLedger.load(CFG)
        a = led.active(dt.date(2026, 1, 6))
        assert a.symbol == "GCG6" and a.rolled_today is False
        assert a.days_to_expiry > 0 and a.expiry > a.date
        rolls = led.roll_dates()
        assert 40 <= len(rolls) <= 60  # ~5-6/year over 8.9 years
        r = rolls[10]
        assert led.crosses_roll(r - dt.timedelta(days=1), r)
        assert not led.crosses_roll(r, r + dt.timedelta(days=1))
        # dev slice (2026-01-04..16) is roll-free — Step 2 selection criterion
        assert not led.crosses_roll(dt.date(2026, 1, 4), dt.date(2026, 1, 16))


class TestRollHelpers:
    def test_symbol_resolution_and_expiry(self):
        from build_roll_ledger import expiry, month_year

        # GCM7 seen on 2017-05-21 -> June 2017
        assert month_year("GCM7", dt.date(2017, 5, 21)) == (2017, 6)
        # GCG6 seen in Jan 2026 -> Feb 2026
        assert month_year("GCG6", dt.date(2026, 1, 4)) == (2026, 2)
        # decade wrap: GCZ9 seen in 2019 vs GCF0 seen late 2019
        assert month_year("GCZ9", dt.date(2019, 11, 1)) == (2019, 12)
        assert month_year("GCF0", dt.date(2019, 11, 1)) == (2020, 1)
        # spreads are not outrights
        assert month_year("GCM7-GCQ7", dt.date(2017, 5, 21)) is None
        # third-to-last weekday: June 2017 -> Jun 28 (Fri 30, Thu 29, Wed 28)
        assert expiry(2017, 6) == dt.date(2017, 6, 28)
        # December wraps the year correctly
        assert expiry(2025, 12) == dt.date(2025, 12, 29)
