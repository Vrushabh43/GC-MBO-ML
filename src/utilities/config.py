"""Typed loader for config/config.toml — the single source of runtime configuration.

Per Engineering Requirements: config files over hardcoding. Every module reads
settings through this loader; nothing re-declares paths or thresholds inline.
"""
from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo root = two levels above this file (src/utilities/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "config.toml"


@dataclass(frozen=True)
class DataConfig:
    dataset: str
    schema: str
    symbols: list[str]
    stype_in: str
    raw_archive_dir: Path
    archive_start: dt.date
    archive_end: dt.date
    known_missing_days: list[dt.date]
    dev_slice_start: dt.date
    dev_slice_end: dt.date


@dataclass(frozen=True)
class StorageConfig:
    processed_dir: Path
    features_dir: Path
    labels_dir: Path
    calendar_dir: Path
    sample_index_dir: Path
    reports_dir: Path


@dataclass(frozen=True)
class PerformanceConfig:
    sustained_events_per_sec: int
    burst_events_per_sec: int
    burst_duration_s: int
    live_p99_event_to_feature_ms: int
    batch_workers: int
    worker_memory_cap_gb: int


@dataclass(frozen=True)
class EngineConfig:
    core: str
    dbn_version_policy: str
    allow_unknown_order_events: bool
    sequence_policy: str
    invariant_policy: str
    store_vs_levels_audit_interval: int
    max_incident_records: int


@dataclass(frozen=True)
class Config:
    data: DataConfig
    storage: StorageConfig
    performance: PerformanceConfig
    engine: EngineConfig
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def _date(v: Any) -> dt.date:
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


def load_config(path: Path | None = None) -> Config:
    p = path or CONFIG_PATH
    with open(p, "rb") as f:
        raw = tomllib.load(f)

    d = raw["data"]
    s = raw["storage"]
    perf = raw["performance"]
    e = raw["engine"]

    def rp(rel: str) -> Path:
        q = Path(rel)
        return q if q.is_absolute() else REPO_ROOT / q

    return Config(
        data=DataConfig(
            dataset=d["dataset"],
            schema=d["schema"],
            symbols=list(d["symbols"]),
            stype_in=d["stype_in"],
            raw_archive_dir=rp(d["raw_archive_dir"]),
            archive_start=_date(d["archive_start"]),
            archive_end=_date(d["archive_end"]),
            known_missing_days=[_date(x) for x in d["known_missing_days"]],
            dev_slice_start=_date(d["dev_slice"]["start"]),
            dev_slice_end=_date(d["dev_slice"]["end"]),
        ),
        storage=StorageConfig(
            processed_dir=rp(s["processed_dir"]),
            features_dir=rp(s["features_dir"]),
            labels_dir=rp(s["labels_dir"]),
            calendar_dir=rp(s["calendar_dir"]),
            sample_index_dir=rp(s["sample_index_dir"]),
            reports_dir=rp(s["reports_dir"]),
        ),
        performance=PerformanceConfig(
            sustained_events_per_sec=perf["sustained_events_per_sec"],
            burst_events_per_sec=perf["burst_events_per_sec"],
            burst_duration_s=perf["burst_duration_s"],
            live_p99_event_to_feature_ms=perf["live_p99_event_to_feature_ms"],
            batch_workers=perf["batch_workers"],
            worker_memory_cap_gb=perf["worker_memory_cap_gb"],
        ),
        engine=EngineConfig(
            core=e["core"],
            dbn_version_policy=e["dbn_version_policy"],
            allow_unknown_order_events=e["allow_unknown_order_events"],
            sequence_policy=e["sequence_policy"],
            invariant_policy=e["invariant_policy"],
            store_vs_levels_audit_interval=e["store_vs_levels_audit_interval"],
            max_incident_records=e["max_incident_records"],
        ),
        raw=raw,
    )
