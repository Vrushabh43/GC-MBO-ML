# Archive inventory audit (Build Order Step 2)

Generated: 2026-07-08T21:37:20+00:00
Archive: `/home/43e3/solr-home/GC/data/raw_mbo/daily`  |  spec: gc_orderflow_plan_v2.md v2.5

## Summary

- Files: **2774**  (2017-05-21 .. 2026-03-31)
- Total compressed size: **112.9 GB**  (min 0.02 MB, median 39.5 MB, max 594.9 MB)
- Metadata readable: **2774/2774**
- DBN versions present: **[1]**
- Dataset/schema uniform: **True**
- Missing non-Saturday days: **1** -> ['2020-02-28']
- Degraded days (vendor): **16**
- Quality mask rows: 2775 (reports/quality_mask.csv)

## DBN version histogram (year x version)

```
date  dbn_version
2017  1              193
2018  1              313
2019  1              313
2020  1              313
2021  1              313
2022  1              312
2023  1              313
2024  1              314
2025  1              313
2026  1               77
dtype: int64
```

## Decode spot-check (30 files, 50000 records each)

```
glbx-mdp3-20170521.mbo.dbn.zst: ok=True decoded=48850 (0.01s) 
glbx-mdp3-20170910.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20171231.mbo.dbn.zst: ok=True decoded=3917 (0.0s) 
glbx-mdp3-20180101.mbo.dbn.zst: ok=True decoded=31890 (0.01s) 
glbx-mdp3-20180702.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20181231.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20190101.mbo.dbn.zst: ok=True decoded=15564 (0.0s) 
glbx-mdp3-20190702.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20191231.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20200101.mbo.dbn.zst: ok=True decoded=40111 (0.01s) 
glbx-mdp3-20200702.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20201231.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20210101.mbo.dbn.zst: ok=True decoded=2919 (0.0s) 
glbx-mdp3-20210702.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20211231.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20220102.mbo.dbn.zst: ok=True decoded=25787 (0.01s) 
glbx-mdp3-20220703.mbo.dbn.zst: ok=True decoded=39082 (0.01s) 
glbx-mdp3-20221230.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20230101.mbo.dbn.zst: ok=True decoded=2419 (0.0s) 
glbx-mdp3-20230702.mbo.dbn.zst: ok=True decoded=32269 (0.01s) 
glbx-mdp3-20231231.mbo.dbn.zst: ok=True decoded=3695 (0.0s) 
glbx-mdp3-20240101.mbo.dbn.zst: ok=True decoded=33778 (0.01s) 
glbx-mdp3-20240702.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20241231.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20250101.mbo.dbn.zst: ok=True decoded=25072 (0.01s) 
glbx-mdp3-20250702.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20251231.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20260101.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20260215.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
glbx-mdp3-20260331.mbo.dbn.zst: ok=True decoded=50000 (0.01s) 
```

## Development slice (Step 2 selection)

- Range: 2026-01-04 .. 2026-01-16
- Sessions present: 12, all status=ok: **True**

## Problems

NONE — archive passes the audit.
