"""Step 12.5 stage 2 — build and VERIFY the full-archive active-contract
ledger (plan Phase 1 contract-roll policy + build order Step 12.5).

Consumes the per-session volume scans (scan_archive_volumes.py) and applies
the configured roll rule — volume_cross: the active contract rolls to the
next-expiry outright after that contract's daily volume exceeds the active
contract's for N consecutive sessions (N = config [roll]
volume_cross_sessions). The roll takes effect the session AFTER the streak
completes, so the decision uses only completed sessions (past-only).

A forced roll (active reached expiry without a volume cross) is flagged —
it should never fire if the volume rule is healthy.

Emits data/calendar/contract_ledger.parquet (one row per session) and the
verification report reports/roll_ledger.md; exits non-zero if any
verification check fails.

Run:  .venv/bin/python scripts/build_roll_ledger.py
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import pandas as pd  # noqa: E402

from utilities.config import load_config  # noqa: E402

OUTRIGHT = re.compile(r"^GC([FGHJKMNQUVXZ])(\d)$")
MONTH = {c: i + 1 for i, c in enumerate("FGHJKMNQUVXZ")}
ACTIVE_CYCLE = set("GJMQVZ")  # Feb Apr Jun Aug Oct Dec — GC's liquid months


def resolve_year(digit: int, session: dt.date) -> int:
    """Single-digit contract year -> full year: the smallest year >=
    session.year - 1 with that last digit whose delivery month can still be
    live around the session (front contracts are always within ~14 months)."""
    for y in range(session.year - 1, session.year + 10):
        if y % 10 == digit:
            return y if y >= session.year - 1 else y + 10
    raise AssertionError


def month_year(symbol: str, session: dt.date) -> tuple[int, int] | None:
    m = OUTRIGHT.match(symbol)
    if not m:
        return None
    month = MONTH[m.group(1)]
    year = resolve_year(int(m.group(2)), session)
    # a resolved contract that already ended >60 days before the session is
    # a decade mis-resolution -> push one decade forward
    if dt.date(year, month, 28) < session - dt.timedelta(days=60):
        year += 10
    return year, month


def expiry(year: int, month: int) -> dt.date:
    """GC termination of trading: third-to-last business day of the
    delivery month (v1 business days = weekdays; documented approximation)."""
    d = dt.date(year + (month == 12), (month % 12) + 1, 1)  # 1st of next month
    seen = 0
    while True:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            seen += 1
            if seen == 3:
                return d


def main() -> int:
    cfg = load_config()
    roll_cfg = cfg.raw["roll"]
    n_cross = int(roll_cfg["volume_cross_sessions"])
    scan_dir = REPO / roll_cfg["scan_dir"]
    ledger_path = REPO / roll_cfg["ledger_file"]

    files = sorted(scan_dir.glob("scan-*.parquet"))
    assert files, f"no scans in {scan_dir} — run scan_archive_volumes.py first"

    # per-session outright volumes
    sessions: list[tuple[dt.date, dict[str, dict]]] = []
    for f in files:
        df = pd.read_parquet(f)
        date = dt.date.fromisoformat(df["date"].iloc[0])
        outs: dict[str, dict] = {}
        for r in df.itertuples():
            my = month_year(r.symbol, date)
            if my is None:
                continue
            outs[r.symbol] = {
                "iid": int(r.instrument_id),
                "vol": int(r.t_volume),
                "records": int(r.records),
                "ym": my,
                "expiry": expiry(*my),
            }
        sessions.append((date, outs))
    sessions.sort(key=lambda s: s[0])

    rows: list[dict] = []
    active: str | None = None
    streak = 0
    pending_roll: str | None = None
    forced_rolls = 0

    for date, outs in sessions:
        total_vol = sum(o["vol"] for o in outs.values()) or 1

        # apply a roll decided at the END of the previous session
        rolled = False
        if pending_roll and pending_roll in outs:
            active, pending_roll, streak, rolled = pending_roll, None, 0, True

        if active is None or active not in outs:
            # first session, or active vanished from the feed
            active = max(outs, key=lambda s: (outs[s]["vol"], s))
            rolled = rolled or len(rows) > 0
        elif date >= outs[active]["expiry"]:
            # safety net: never carry an expired active (flagged, counted)
            active = max(outs, key=lambda s: (outs[s]["vol"], s))
            forced_rolls += 1
            rolled = True
            streak = 0

        a = outs[active]
        # Roll candidate = the VOLUME SUCCESSOR: highest-volume outright with
        # a later expiry. DOCUMENTED DEVIATION from the plan's literal "next
        # month": GC's calendar-adjacent month is usually an illiquid dead
        # month (Jul/Sep/...), and the literal rule ladders the active
        # contract through it with ~0% volume share — contradicting the
        # plan's own front-by-volume definition. Verified in the report.
        later = [s for s, o in outs.items() if o["expiry"] > a["expiry"]]
        nxt = max(later, key=lambda s: (outs[s]["vol"], s)) if later else None
        nvol = outs[nxt]["vol"] if nxt else 0

        streak = streak + 1 if (nxt and nvol > a["vol"]) else 0
        if streak >= n_cross:
            pending_roll = nxt  # takes effect NEXT session (past-only)

        leader = max(outs, key=lambda s: (outs[s]["vol"], s))
        rows.append(
            {
                "date": date,
                "active_symbol": active,
                "active_instrument_id": a["iid"],
                "active_volume": a["vol"],
                "active_share": a["vol"] / total_vol,
                "active_expiry": a["expiry"],
                "days_to_expiry": (a["expiry"] - date).days,
                "next_symbol": nxt,
                "next_volume": nvol,
                "cross_streak": streak,
                "rolled_today": rolled,
                "leader_symbol": leader,
                "leader_is_active": leader == active,
            }
        )

    led = pd.DataFrame(rows)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    led.to_parquet(ledger_path, index=False)

    # ---------------- verification (gate) ---------------------------------
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    check("all sessions have an active outright",
          led["active_symbol"].str.match(OUTRIGHT).all(),
          f"{len(led)} sessions")
    dev = led[(led["date"] >= dt.date(2026, 1, 4)) & (led["date"] <= dt.date(2026, 1, 16))]
    check("dev slice active == GCG6 (known ground truth)",
          (dev["active_symbol"] == "GCG6").all(),
          dev["active_symbol"].unique().tolist().__repr__())
    check("active never expired", (led["days_to_expiry"] >= 0).all(),
          f"min days_to_expiry = {led['days_to_expiry'].min()}")
    check("no forced rolls (volume rule always led)", forced_rolls == 0,
          f"forced_rolls = {forced_rolls}")

    rolls = led[led["rolled_today"]]
    exp_seq = pd.to_datetime(rolls["active_expiry"])
    check("roll targets strictly later expiries", exp_seq.is_monotonic_increasing,
          f"{len(rolls)} rolls")
    years = (led["date"].max() - led["date"].min()).days / 365.25
    check("roll cadence ~6/year (bimonthly cycle)",
          4.5 <= len(rolls) / years <= 7.5,
          f"{len(rolls)} rolls over {years:.1f}y = {len(rolls)/years:.1f}/y")
    cyc = rolls["active_symbol"].str[2].isin(ACTIVE_CYCLE)
    check("rolls target the liquid GJMQVZ cycle", cyc.mean() >= 0.95,
          f"{cyc.mean():.1%} in cycle")
    check("active dominates volume (median share > 0.7)",
          led["active_share"].median() > 0.7,
          f"median share = {led['active_share'].median():.2f}")
    check("leader == active on >= 93% of sessions",
          led["leader_is_active"].mean() >= 0.93,
          f"{led['leader_is_active'].mean():.1%}")
    ok_all = all(ok for _, ok, _ in checks)

    # ---------------- report ----------------------------------------------
    lines = [
        "# Step 12.5 — active-contract ledger (full archive)",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
        f"Sessions: {led['date'].min()} .. {led['date'].max()} ({len(led):,}) | "
        f"rule: volume_cross x{n_cross} (past-only, effective next session) | "
        f"ledger: `{ledger_path.relative_to(REPO)}`",
        "",
        "## Verification",
        "",
        "| check | result | detail |",
        "|---|---|---|",
    ]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'PASS' if ok else '**FAIL**'} | {detail} |")
    lines += [
        "",
        f"## Rolls ({len(rolls)})",
        "",
        "| date | new active | days-to-expiry at roll | active share that day |",
        "|---|---|---|---|",
    ]
    for r in rolls.itertuples():
        lines.append(
            f"| {r.date} | {r.active_symbol} | {r.days_to_expiry} | {r.active_share:.2f} |"
        )
    lines += [
        "",
        "Expiry rule: third-to-last WEEKDAY of the delivery month (v1 — CME",
        "holiday calendar not yet ingested; can shift days_to_expiry by a day",
        "or two around holidays, never the roll decision, which is",
        "volume-driven). At a roll, downstream consumers must reset rolling",
        "windows, normalization state, and regime percentiles, and never",
        "stitch features or labels across contracts (plan Phase 1 rule).",
        "",
    ]
    out = REPO / "reports" / "roll_ledger.md"
    out.write_text("\n".join(lines))
    print("\n".join(lines[: 20 + len(checks)]))
    print(f"\nfull report -> {out}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
