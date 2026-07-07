"""Sequential batch downloader for the GC MBO archive.

Reads keys/key-N.txt (line 1 = API key, line 2 = date range, possibly reversed),
and for each key IN ORDER: submits a Databento batch job, waits for it to finish
packaging, downloads all daily .dbn.zst files, and verifies them. Strictly one
key at a time. Restartable: a done-marker per key lets a re-run skip finished work.
A failed key is logged and skipped; the loop continues with the next key.
"""
import os
import sys
import time
import json
import glob
import datetime as dt

import databento as db

BASE = "/home/43e3/solr-home/GC"
KEYS_DIR = f"{BASE}/keys"
OUT_DIR = f"{BASE}/data/raw_mbo"
DONE_DIR = f"{KEYS_DIR}/.done"
LOG_PATH = f"{BASE}/data/raw_mbo/download_archive.log"

DATASET = "GLBX.MDP3"
SCHEMA = "mbo"
SYMBOLS = ["GC.FUT"]
STYPE_IN = "parent"

POLL_S = 120
MAX_WAIT_S = 6 * 3600
ARCHIVE_FLOOR = dt.date(2017, 5, 21)  # first MBO day; nothing exists before this

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(DONE_DIR, exist_ok=True)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def parse_key_file(path):
    with open(path) as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    key = lines[0]
    d1, d2 = [dt.date.fromisoformat(x.strip()) for x in lines[1].replace(" to ", "|").split("|")]
    start = min(d1, d2)
    end_incl = max(d1, d2)
    if start < ARCHIVE_FLOOR:
        start = ARCHIVE_FLOOR
    return key, start, end_incl


def verify_job(job_dir, start, end_incl):
    problems = []
    # 1. manifest size check
    with open(f"{job_dir}/manifest.json") as f:
        m = json.load(f)
    entries = m.get("files", m)
    data_files = [e for e in entries if e["filename"].endswith(".dbn.zst")]
    for e in data_files:
        fp = os.path.join(job_dir, e["filename"])
        if not os.path.exists(fp) or os.path.getsize(fp) != e["size"]:
            problems.append(f"size/existence mismatch: {e['filename']}")
    # 2. calendar completeness (absent weekday that is not Saturday = problem)
    have = set()
    for p in glob.glob(f"{job_dir}/glbx-mdp3-*.mbo.dbn.zst"):
        f = os.path.basename(p)
        have.add(dt.date.fromisoformat(f"{f[10:14]}-{f[14:16]}-{f[16:18]}"))
    d = start
    absent_non_sat = []
    while d <= end_incl:
        if d not in have and d.strftime("%A") != "Saturday":
            absent_non_sat.append(d.isoformat())
        d += dt.timedelta(days=1)
    if absent_non_sat:
        problems.append(f"absent non-Saturday days: {absent_non_sat}")
    # 3. decode test on one file
    if have:
        sample = sorted(glob.glob(f"{job_dir}/glbx-mdp3-*.mbo.dbn.zst"))[0]
        try:
            store = db.DBNStore.from_file(sample)
            it = iter(store)
            for _ in range(10000):
                next(it, None)
        except Exception as e:
            problems.append(f"decode test failed on {os.path.basename(sample)}: {e}")
    return len(have), problems


def process_key(n):
    marker = f"{DONE_DIR}/key-{n}.done"
    if os.path.exists(marker):
        log(f"key-{n}: already done (marker present), skipping.")
        return True

    key, start, end_incl = parse_key_file(f"{KEYS_DIR}/key-{n}.txt")
    end_excl = end_incl + dt.timedelta(days=1)
    log(f"key-{n}: range {start} .. {end_incl} (inclusive); requesting end={end_excl} exclusive.")

    client = db.Historical(key)

    try:
        cost = client.metadata.get_cost(
            dataset=DATASET, symbols=SYMBOLS, schema=SCHEMA,
            start=start.isoformat(), end=end_excl.isoformat(), stype_in=STYPE_IN)
        size = client.metadata.get_billable_size(
            dataset=DATASET, symbols=SYMBOLS, schema=SCHEMA,
            start=start.isoformat(), end=end_excl.isoformat(), stype_in=STYPE_IN)
        log(f"key-{n}: estimated cost ${cost:,.2f}, {size/1e9:.1f} GB uncompressed.")
    except Exception as e:
        log(f"key-{n}: cost/metadata check FAILED: {e} -- skipping this key.")
        return False

    try:
        job = client.batch.submit_job(
            dataset=DATASET, schema=SCHEMA, symbols=SYMBOLS, stype_in=STYPE_IN,
            start=start.isoformat(), end=end_excl.isoformat(),
            encoding="dbn", compression="zstd", split_duration="day")
        job_id = job["id"]
        log(f"key-{n}: submitted job {job_id}, state={job['state']}.")
    except Exception as e:
        log(f"key-{n}: submit FAILED: {e} -- skipping this key.")
        return False

    deadline = time.time() + MAX_WAIT_S
    state = None
    while time.time() < deadline:
        try:
            jobs = {j["id"]: j for j in client.batch.list_jobs(since="2026-07-06")}
            job = jobs.get(job_id)
            state = job["state"] if job else "unknown"
        except Exception as e:
            log(f"key-{n}: list_jobs error (will retry): {e}")
            time.sleep(POLL_S)
            continue
        if state == "done":
            log(f"key-{n}: job done. records={job.get('record_count')} "
                f"cost_usd={job.get('cost_usd')}")
            break
        if state == "expired":
            log(f"key-{n}: job expired before download -- skipping this key.")
            return False
        time.sleep(POLL_S)
    else:
        log(f"key-{n}: timed out waiting (last state={state}) -- skipping this key.")
        return False

    for attempt in range(3):
        try:
            paths = client.batch.download(job_id=job_id, output_dir=OUT_DIR)
            log(f"key-{n}: downloaded {len(paths)} files to {OUT_DIR}/{job_id}/.")
            break
        except Exception as e:
            log(f"key-{n}: download attempt {attempt+1} failed: {e}")
            time.sleep(60)
    else:
        log(f"key-{n}: download FAILED after retries -- skipping this key.")
        return False

    job_dir = f"{OUT_DIR}/{job_id}"
    n_files, problems = verify_job(job_dir, start, end_incl)
    if problems:
        log(f"key-{n}: VERIFICATION PROBLEMS ({n_files} files): {problems}")
        return False
    log(f"key-{n}: VERIFIED OK -- {n_files} daily files, {start}..{end_incl}.")
    with open(marker, "w") as f:
        f.write(job_id + "\n")
    return True


def main():
    log("=" * 70)
    log("Starting sequential archive download for keys 1..8.")
    results = {}
    for n in range(1, 9):
        if not os.path.exists(f"{KEYS_DIR}/key-{n}.txt"):
            log(f"key-{n}: file not found, skipping.")
            results[n] = "missing-file"
            continue
        ok = process_key(n)
        results[n] = "OK" if ok else "FAILED"
    log("-" * 70)
    log(f"FINAL SUMMARY: {results}")

    # combined archive report
    have = {}
    dup = []
    for p in glob.glob(f"{OUT_DIR}/GLBX-*/glbx-mdp3-*.mbo.dbn.zst"):
        f = os.path.basename(p)
        d = dt.date.fromisoformat(f"{f[10:14]}-{f[14:16]}-{f[16:18]}")
        if d in have:
            dup.append(d.isoformat())
        have[d] = p
    if have:
        log(f"COMBINED ARCHIVE: {len(have)} daily files, {min(have)} .. {max(have)}, "
            f"duplicates={dup if dup else 'none'}")
    log("ALL DONE.")


if __name__ == "__main__":
    main()
