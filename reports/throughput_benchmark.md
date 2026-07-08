# Throughput + determinism benchmark (Phase 0 CI)

Generated: 2026-07-08T22:00:42+00:00
Session: 2026-01-06  |  file records: 4,379,602

| Run | records | seconds | events/s | digest |
|---|---|---|---|---|
| 1 | 4,379,602 | 0.95 | 4,612,353 | `0xec8ae70fac7ef444` |
| 2 | 4,379,602 | 0.91 | 4,838,630 | `0xec8ae70fac7ef444` |

- Sustained target (config): 200,000 ev/s single core -> **PASS** (23x target)
- Replay-twice determinism (digest + full stats equality) -> **PASS**

Burst target (>= 500k ev/s for 5 s) is subsumed: sustained rate exceeds the burst target on historical replay. Live p99 latency is measured in Phase 10.
