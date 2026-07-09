"""Phase 2 — order lifecycle and queue engine (Python API over gc_core).

The compiled core emits one record per order at its terminal event (filled /
pulled / cleared / end-of-data), carrying queue positions, distances from the
market at add and termination, closest same-side-best approach while resting
("survival as price approached"), Globex-priority modify counts, and the
iceberg synthetic-parent chain linkage. This module turns the drained columns
into DataFrames, derives the chain table, and computes neutral-named session
diagnostics (plan Phase 2: never label behavior as intent — no "spoofing").

Everything here is per-session output for Phase 3 feature construction; raw
values only — sigma/v_scale normalized twins are a Phase 4 concern.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from mbo_engine.engine import MboEngine, PRICE_SCALE, ReplayResult
from utilities.config import Config, load_config

# terminal states (mirrors gc_core; neutral mechanics, never intent)
STATE_NAMES = {
    0: "filled",
    1: "partial_cancelled",
    2: "cancelled",
    3: "cleared",
    4: "end_of_data",
    5: "replaced",
}
POS_SENTINEL = np.uint32(0xFFFF_FFFF)
DIST_SENTINEL = np.iinfo(np.int64).min
MIN_DIST_SENTINEL = np.iinfo(np.int64).max
VOL_SENTINEL = np.uint64(0xFFFF_FFFF_FFFF_FFFF)


@dataclass(frozen=True)
class LifecycleSession:
    date: dt.date
    replay: ReplayResult
    orders: pd.DataFrame
    chains: pd.DataFrame
    lifecycle_digest: int
    stats: dict[str, int]


def _tick_units(cfg: Config) -> int:
    """One tick expressed in DBN fixed-point price units."""
    tick_pts = float(cfg.raw["costs"]["tick_size_pts"])
    return int(round(tick_pts / PRICE_SCALE))


def orders_frame(cols: dict[str, np.ndarray], cfg: Config) -> pd.DataFrame:
    """Drained lifecycle columns -> typed DataFrame with derived fields.

    Distances arrive in fixed-point price units; `_ticks` twins are derived
    (raw only — normalization is Phase 4). Sentinels become NaN/NA.
    """
    tick = _tick_units(cfg)
    df = pd.DataFrame(cols)

    df["side"] = df["side"].map({ord("B"): "B", ord("A"): "A"})
    df["final_state"] = df["final_state"].map(STATE_NAMES)
    df["from_snapshot"] = df["from_snapshot"].astype(bool)
    df["entered_unknown_modify"] = df["entered_unknown_modify"].astype(bool)
    df["lifetime_ns"] = df["ts_terminated"] - df["ts_added"]

    # sentinels -> missing
    df["queue_pos_at_term"] = df["queue_pos_at_term"].where(
        df["queue_pos_at_term"] != POS_SENTINEL
    )
    df["queue_pos_at_add"] = df["queue_pos_at_add"].where(
        df["queue_pos_at_add"] != POS_SENTINEL
    )
    df["vol_ahead_at_add"] = df["vol_ahead_at_add"].where(
        df["vol_ahead_at_add"] != VOL_SENTINEL
    )
    for c in ("dist_same_at_add", "dist_mid2_at_add", "dist_same_at_term",
              "dist_mid2_at_term"):
        df[c] = df[c].where(df[c] != DIST_SENTINEL)
    df["min_dist_same"] = df["min_dist_same"].where(
        df["min_dist_same"] != MIN_DIST_SENTINEL
    )

    # tick-distance twins (mid distances are stored doubled to stay integer)
    df["dist_same_at_add_ticks"] = df["dist_same_at_add"] / tick
    df["dist_mid_at_add_ticks"] = df["dist_mid2_at_add"] / (2 * tick)
    df["dist_same_at_term_ticks"] = df["dist_same_at_term"] / tick
    df["dist_mid_at_term_ticks"] = df["dist_mid2_at_term"] / (2 * tick)
    df["min_dist_same_ticks"] = df["min_dist_same"] / tick

    # survival mechanics: did the same-side best ever reach the order's level
    # (or the order execute) while it rested?
    df["touched_best"] = (df["min_dist_same"] <= 0) | (df["filled_size"] > 0)
    df["cancel_before_touch"] = (
        df["final_state"].isin(["cancelled"]) & ~df["touched_best"]
    )
    df["is_refill_link"] = df["chain_index"] > 0
    return df


def chains_frame(orders: pd.DataFrame) -> pd.DataFrame:
    """Synthetic-parent chain table (iceberg refill heuristic, Critical
    Rule 8: confidence stored, never treated as fact).

    A chain is >= 2 records sharing a chain_id (root = the first exhausted
    displayed clip; children = linked refills).
    """
    linked = orders[orders["chain_id"] > 0]
    if linked.empty:
        return pd.DataFrame()
    g = linked.groupby("chain_id")
    chains = pd.DataFrame(
        {
            "instrument_id": g["instrument_id"].first(),
            "side": g["side"].first(),
            "members": g.size(),
            "refills": g["chain_index"].max(),
            "total_displayed": g["initial_size"].sum(),
            "total_filled": g["filled_size"].sum(),
            "first_ts": g["ts_added"].min(),
            "last_ts": g["ts_terminated"].max(),
            "min_link_confidence": g.apply(
                lambda x: x.loc[x["chain_index"] > 0, "link_confidence"].min(),
                include_groups=False,
            ),
            "max_link_dt_ns": g["link_dt_ns"].max(),
        }
    )
    chains = chains[chains["members"] >= 2].copy()
    if chains.empty:
        return chains
    chains["executed_to_displayed_ratio"] = (
        chains["total_filled"] / chains["total_displayed"]
    )
    chains["duration_ns"] = chains["last_ts"] - chains["first_ts"]
    return chains.reset_index()


def session_diagnostics(
    orders: pd.DataFrame, instrument_id: int | None = None
) -> dict[str, float]:
    """Neutral-named session-level lifecycle diagnostics (plan Phase 2).

    These are descriptive session aggregates; the rolling/windowed feature
    versions are built in Phase 3. Naming is mechanical by design.
    Snapshot-entered orders are excluded from add-anchored statistics (their
    true entry time predates the file).
    """
    df = orders
    if instrument_id is not None:
        df = df[df["instrument_id"] == instrument_id]
    live = df[~df["from_snapshot"]]
    pulled = live[live["final_state"].isin(["cancelled", "partial_cancelled"])]
    filled = live[live["final_state"] == "filled"]
    approached = live[live["min_dist_same_ticks"].notna()]
    # orders the market came within 1 tick of (execution-imminent zone)
    near = approached[approached["min_dist_same_ticks"] <= 1]
    large_cut = live["initial_size"].quantile(0.90) if len(live) else np.nan
    large = live[live["initial_size"] >= large_cut] if len(live) else live
    raw_life = live[live["final_state"].isin(["filled", "cancelled",
                                              "partial_cancelled"])]
    unchained_life = raw_life[~raw_life["is_refill_link"]]

    def rate(num: int, den: int) -> float:
        return float(num) / den if den else float("nan")

    return {
        "orders_terminated": float(len(df)),
        "orders_entered_live": float(len(live)),
        "fill_rate": rate(len(filled), len(live)),
        "cancel_before_touch_rate": rate(
            int(pulled["cancel_before_touch"].sum()), len(pulled)
        ),
        # of orders the market approached to <= 1 tick: fraction that stayed
        # (filled or still present) rather than being pulled untouched
        "liquidity_survival_ratio": rate(
            int((~near["cancel_before_touch"]).sum()), len(near)
        ),
        # large displayed orders (>= p90 size) pulled untouched within 1s
        "short_lived_large_order_behavior": rate(
            int(
                (
                    large["cancel_before_touch"]
                    & (large["lifetime_ns"] < 1_000_000_000)
                ).sum()
            ),
            len(large),
        ),
        "median_lifetime_ms_raw": float(raw_life["lifetime_ns"].median()) / 1e6
        if len(raw_life)
        else float("nan"),
        # chain-adjusted twin: refill links excluded so iceberg clips do not
        # masquerade as short-lived orders (plan Phase 2 requirement)
        "median_lifetime_ms_chain_adjusted": float(
            unchained_life["lifetime_ns"].median()
        )
        / 1e6
        if len(unchained_life)
        else float("nan"),
        "refill_link_share_of_adds": rate(
            int(live["is_refill_link"].sum()), len(live)
        ),
    }


def replay_session_lifecycle(
    date: dt.date, cfg: Config | None = None
) -> LifecycleSession:
    """Replay one session with lifecycle tracking; return orders + chains."""
    cfg = cfg or load_config()
    eng = MboEngine(cfg, lifecycle=True)
    replay = eng.replay_date(date)
    orders = orders_frame(eng.lifecycle_drain(), cfg)
    # attach contract symbols for the analytics layer
    orders["symbol"] = orders["instrument_id"].map(
        lambda i: eng.symbol(int(i))
    )
    digest = eng.lifecycle_digest()
    assert digest is not None
    return LifecycleSession(
        date=date,
        replay=replay,
        orders=orders,
        chains=chains_frame(orders),
        lifecycle_digest=digest,
        stats=eng.lifecycle_stats(),
    )


def write_session_lifecycle(
    date: dt.date, cfg: Config | None = None
) -> tuple[Path, Path | None, LifecycleSession]:
    """Replay + persist one session's lifecycle output as Parquet."""
    cfg = cfg or load_config()
    ses = replay_session_lifecycle(date, cfg)
    out_dir = cfg.lifecycle.lifecycle_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ymd = date.strftime("%Y%m%d")
    orders_path = out_dir / f"lifecycle-{ymd}.parquet"
    ses.orders.to_parquet(orders_path, index=False)
    chains_path = None
    if len(ses.chains):
        chains_path = out_dir / f"chains-{ymd}.parquet"
        ses.chains.to_parquet(chains_path, index=False)
    return orders_path, chains_path, ses
