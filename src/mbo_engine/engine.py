"""Thin Python API over the compiled core (plan: src/mbo_engine wraps core/).

Everything performance-critical lives in gc_core (Rust). This wrapper adds:
  - config-driven construction (config/config.toml),
  - DBN-metadata symbology (instrument_id <-> raw contract symbol),
  - front-contract identification by traded volume,
  - convenience accessors returning plain Python structures.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import databento as db
import gc_core

from utilities.config import Config, load_config

PRICE_SCALE = 1e-9  # DBN fixed-point: 1 unit = 1e-9


@dataclass(frozen=True)
class ReplayResult:
    path: Path
    records: int
    seconds: float
    skipped_non_mbo: int
    events_per_sec: float
    digest: int


class MboEngine:
    """Order-book engine for one replay stream (historical file or live push)."""

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or load_config()
        e = self.cfg.engine
        self._core = gc_core.MboEngine(
            max_incidents=e.max_incident_records,
            audit_interval=e.store_vs_levels_audit_interval,
            halt_on_engine_invariant=(e.invariant_policy == "halt"),
        )
        self._symbol_by_iid: dict[int, str] = {}

    # -- symbology ---------------------------------------------------------

    def load_symbology(self, dbn_path: Path) -> None:
        """Read instrument_id -> raw symbol mapping from a DBN file's metadata."""
        store = db.DBNStore.from_file(dbn_path)
        for raw_symbol, intervals in store.metadata.mappings.items():
            for iv in intervals:
                sym = iv["symbol"] if isinstance(iv, dict) else iv.symbol
                if str(sym).isdigit():
                    self._symbol_by_iid[int(sym)] = raw_symbol

    def symbol(self, instrument_id: int) -> str:
        return self._symbol_by_iid.get(instrument_id, f"iid:{instrument_id}")

    # -- processing --------------------------------------------------------

    def replay_file(self, path: Path) -> ReplayResult:
        self.load_symbology(path)
        records, seconds, skipped = self._core.replay_file(str(path))
        return ReplayResult(
            path=path,
            records=records,
            seconds=seconds,
            skipped_non_mbo=skipped,
            events_per_sec=records / seconds if seconds > 0 else 0.0,
            digest=self._core.state_digest(),
        )

    def replay_date(self, date: dt.date) -> ReplayResult:
        from databento_io.sessions import session_file

        return self.replay_file(session_file(date, self.cfg))

    def push(self, *args, **kwargs) -> bool:
        return self._core.push(*args, **kwargs)

    def finish(self) -> None:
        self._core.finish()

    # -- queries -----------------------------------------------------------

    def front_instrument(self) -> int:
        """Most-traded instrument by T volume (the active outright)."""
        rows = self._core.instruments()
        if not rows:
            raise RuntimeError("engine has processed no records")
        return max(rows, key=lambda r: r[2])[0]

    def instruments(self) -> list[tuple[int, str, int, int]]:
        """(instrument_id, symbol, records, t_volume), highest volume first."""
        rows = self._core.instruments()
        rows.sort(key=lambda r: r[2], reverse=True)
        return [(iid, self.symbol(iid), n, v) for iid, n, v in rows]

    def top_levels(
        self, instrument_id: int, side: str, n: int = 10, from_orders: bool = False
    ) -> list[tuple[float, int, int]]:
        """[(price_pts, total_size, order_count)] best-first."""
        raw = self._core.top_levels(instrument_id, side, n, from_orders)
        return [(p * PRICE_SCALE, s, c) for p, s, c in raw]

    def top_levels_raw(
        self, instrument_id: int, side: str, n: int = 10, from_orders: bool = False
    ) -> list[tuple[int, int, int]]:
        return self._core.top_levels(instrument_id, side, n, from_orders)

    def best_bid_ask(self, instrument_id: int):
        return self._core.best_bid_ask(instrument_id)

    def spread_pts(self, instrument_id: int) -> float | None:
        bba = self._core.best_bid_ask(instrument_id)
        if bba is None:
            return None
        (bid_px, _, _), (ask_px, _, _) = bba
        return (ask_px - bid_px) * PRICE_SCALE

    def order(self, instrument_id: int, order_id: int):
        return self._core.order(instrument_id, order_id)

    def queue_position(self, instrument_id: int, order_id: int) -> int | None:
        return self._core.queue_position(instrument_id, order_id)

    def order_count(self, instrument_id: int) -> int:
        return self._core.order_count(instrument_id)

    def stats(self) -> dict[str, int]:
        return dict(self._core.stats())

    def incidents(self, limit: int = 100):
        return self._core.incidents(limit)

    def state_digest(self) -> int:
        return self._core.state_digest()

    def views_consistent(self, instrument_id: int) -> str | None:
        """None if the order-store and level views agree exactly (R1)."""
        return self._core.views_consistent(instrument_id)

    def halted(self) -> str | None:
        return self._core.halted()
