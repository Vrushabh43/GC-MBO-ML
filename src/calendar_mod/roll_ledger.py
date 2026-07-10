"""Active-contract ledger accessor (Step 12.5 consumer API).

Downstream phases read the ledger through this module only:
  - which contract is ACTIVE on a session date (train on this, nothing else)
  - days-to-expiry (stored with every sample, plan Phase 1)
  - roll boundaries — rolling windows, normalization state, and regime
    percentiles must RESET at a roll, and features/labels must never
    stitch across one (plan Phase 1 roll policy).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from utilities.config import Config, REPO_ROOT


@dataclass(frozen=True)
class ActiveContract:
    date: dt.date
    symbol: str
    instrument_id: int
    days_to_expiry: int
    expiry: dt.date
    rolled_today: bool


class RollLedger:
    def __init__(self, df: pd.DataFrame) -> None:
        self._by_date: dict[dt.date, ActiveContract] = {}
        for r in df.itertuples():
            d = r.date if isinstance(r.date, dt.date) else r.date.date()
            e = r.active_expiry if isinstance(r.active_expiry, dt.date) else r.active_expiry.date()
            self._by_date[d] = ActiveContract(
                date=d,
                symbol=r.active_symbol,
                instrument_id=int(r.active_instrument_id),
                days_to_expiry=int(r.days_to_expiry),
                expiry=e,
                rolled_today=bool(r.rolled_today),
            )

    @classmethod
    def load(cls, cfg: Config) -> "RollLedger":
        p = Path(cfg.raw["roll"]["ledger_file"])
        if not p.is_absolute():
            p = REPO_ROOT / p
        if not p.exists():
            raise FileNotFoundError(
                f"{p} — build it: scripts/scan_archive_volumes.py then "
                f"scripts/build_roll_ledger.py"
            )
        return cls(pd.read_parquet(p))

    def active(self, date: dt.date) -> ActiveContract:
        """Active contract for a session date (KeyError if not a session)."""
        return self._by_date[date]

    def roll_dates(self) -> list[dt.date]:
        return sorted(d for d, a in self._by_date.items() if a.rolled_today)

    def crosses_roll(self, d0: dt.date, d1: dt.date) -> bool:
        """True if [d0, d1] spans a roll boundary — such intervals must
        never feed a rolling window, feature, or label (plan Phase 1)."""
        return any(d0 < r <= d1 for r in self.roll_dates())
