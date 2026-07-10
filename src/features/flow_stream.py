"""Session driver for the Phase 3 flow-primitive stream.

Historical path: replay a session with the flow recorder attached to the
front contract and drain the per-group primitive columns. The front contract
is identified by a cheap first replay (T-volume maximum, same rule as
Phase 1); the full-archive roll ledger (Step 12.5) will replace that lookup
later.

The drained columns feed FeatureEngine.step(cols, i) row by row — the same
step() a live session calls with length-1 columns.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from mbo_engine.engine import MboEngine, ReplayResult
from utilities.config import Config, load_config


@dataclass(frozen=True)
class FlowSession:
    date: dt.date
    instrument_id: int
    symbol: str
    replay: ReplayResult
    cols: dict[str, np.ndarray]

    @property
    def rows(self) -> int:
        return len(self.cols["ts"])


def front_instrument(date: dt.date, cfg: Config) -> int:
    """Front contract by traded volume (Phase 1 rule) via a plain replay."""
    e = MboEngine(cfg, lifecycle=False)
    e.replay_date(date)
    return e.front_instrument()


def replay_session_flow(
    date: dt.date,
    cfg: Config | None = None,
    instrument_id: int | None = None,
) -> FlowSession:
    """Replay one session recording flow primitives for one instrument
    (default: the session's front contract)."""
    cfg = cfg or load_config()
    iid = instrument_id if instrument_id is not None else front_instrument(date, cfg)
    e = MboEngine(cfg, lifecycle=True)
    e.enable_flow(iid)
    r = e.replay_date(date)
    return FlowSession(
        date=date,
        instrument_id=iid,
        symbol=e.symbol(iid),
        replay=r,
        cols=e.flow_drain(),
    )
