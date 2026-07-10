"""Step 12.5 stage 1 — full-archive per-instrument volume scan.

For every session file: per-instrument record and T-volume tallies (lean
Rust scanner, no book) + instrument_id -> raw symbol mapping from the DBN
metadata. One parquet per session in data/processed/roll_scan/ — the scan
is RESUMABLE (existing outputs are skipped) and parallel (one session per
worker, hardware profile: ~10 workers).

The ledger builder (build_roll_ledger.py) consumes these outputs.

Run:  .venv/bin/python scripts/scan_archive_volumes.py [n_workers]
"""
from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from utilities.config import load_config  # noqa: E402

OUT_DIR = REPO / "data" / "processed" / "roll_scan"


def scan_one(path_str: str) -> tuple[str, int, str]:
    """Worker: scan one session file -> per-instrument parquet."""
    import databento as db
    import gc_core
    import pandas as pd

    path = Path(path_str)
    ymd = path.name.split("-")[2].split(".")[0]
    out = OUT_DIR / f"scan-{ymd}.parquet"

    rows = gc_core.scan_t_volumes(str(path))

    sym_by_iid: dict[int, str] = {}
    store = db.DBNStore.from_file(path)
    for raw_symbol, intervals in store.metadata.mappings.items():
        for iv in intervals:
            s = iv["symbol"] if isinstance(iv, dict) else iv.symbol
            if str(s).isdigit():
                sym_by_iid[int(s)] = raw_symbol

    df = pd.DataFrame(rows, columns=["instrument_id", "records", "t_volume"])
    df.insert(0, "date", f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}")
    df["symbol"] = df["instrument_id"].map(lambda i: sym_by_iid.get(i, f"iid:{i}"))
    df.to_parquet(out, index=False)
    return ymd, int(df["records"].sum()), str(out)


def main() -> int:
    cfg = load_config()
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else cfg.performance.batch_workers
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(cfg.data.raw_archive_dir.glob("glbx-mdp3-*.mbo.dbn.zst"))
    todo = []
    for f in files:
        ymd = f.name.split("-")[2].split(".")[0]
        if not (OUT_DIR / f"scan-{ymd}.parquet").exists():
            todo.append(f)
    print(f"{len(files)} sessions total, {len(todo)} to scan, {workers} workers")

    done = errs = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scan_one, str(f)): f for f in todo}
        for fut in as_completed(futs):
            try:
                ymd, n, _ = fut.result()
                done += 1
                if done % 100 == 0 or done == len(todo):
                    print(f"[{done}/{len(todo)}] {ymd}: {n:,} records")
            except Exception as e:  # noqa: BLE001
                errs += 1
                print(f"ERROR {futs[fut].name}: {e}")
    print(f"done: {done} scanned, {errs} errors")
    return 0 if errs == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
