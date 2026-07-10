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

    def __init__(self, cfg: Config | None = None, lifecycle: bool | None = None) -> None:
        self.cfg = cfg or load_config()
        e = self.cfg.engine
        lc = self.cfg.lifecycle
        self._core = gc_core.MboEngine(
            max_incidents=e.max_incident_records,
            audit_interval=e.store_vs_levels_audit_interval,
            halt_on_engine_invariant=(e.invariant_policy == "halt"),
            lifecycle=lc.enabled if lifecycle is None else lifecycle,
            iceberg_window_ns=lc.iceberg_link_window_ns,
            iceberg_clip_tol=lc.iceberg_clip_tolerance,
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

    # -- Phase 2: order lifecycle + queue engine -----------------------------

    def volume_ahead(self, instrument_id: int, order_id: int) -> int | None:
        """Volume resting ahead of the order in its level's FIFO."""
        return self._core.volume_ahead(instrument_id, order_id)

    def level_ages(
        self, instrument_id: int, side: str, price: int, ts_now: int
    ) -> list[tuple[int, int, int, bool]]:
        """Liquidity ages at a level, front-first:
        (order_id, age_ns, current_size, from_snapshot)."""
        return self._core.level_ages(instrument_id, side, price, ts_now)

    def lifecycle_len(self) -> int:
        return self._core.lifecycle_len()

    def lifecycle_digest(self) -> int | None:
        """Deterministic digest over all emitted lifecycle records."""
        return self._core.lifecycle_digest()

    def lifecycle_stats(self) -> dict[str, int]:
        """records_emitted / iceberg_links_made / refill_slots_opened."""
        s = self._core.lifecycle_stats()
        if s is None:
            return {}
        return {
            "records_emitted": s[0],
            "iceberg_links_made": s[1],
            "refill_slots_opened": s[2],
        }

    def lifecycle_drain(self) -> dict[str, "np.ndarray"]:
        """Take all completed lifecycle records as numpy column arrays."""
        import numpy as np

        return {
            name: np.frombuffer(buf, dtype=np.dtype(dt))
            for name, dt, buf in self._core.lifecycle_drain()
        }

    # -- Phase 3: per-group flow primitives ----------------------------------

    def enable_flow(self, instrument_id: int) -> None:
        """Record per-matching-event-group flow primitives for one
        instrument (config [features]; requires lifecycle tracking)."""
        ft = self.cfg.features
        tick_units = int(round(float(self.cfg.raw["costs"]["tick_size_pts"]) / PRICE_SCALE))
        self._core.enable_flow(
            instrument_id,
            tick_units,
            ft.near_touch_ticks,
            ft.book_levels,
        )

    def flow_stats(self) -> tuple[int, int] | None:
        """(rows buffered, groups emitted total) or None if not enabled."""
        return self._core.flow_stats()

    def flow_drain(self) -> dict[str, "np.ndarray"]:
        """Take buffered flow-primitive rows as numpy column arrays."""
        import numpy as np

        return {
            name: np.frombuffer(buf, dtype=np.dtype(dt))
            for name, dt, buf in self._core.flow_drain()
        }
