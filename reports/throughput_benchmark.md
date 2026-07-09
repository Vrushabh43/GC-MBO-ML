# Throughput + determinism benchmark (Phase 0 CI)

Generated: 2026-07-09T21:05:47+00:00
Session: 2026-01-06  |  file records: 4,379,602

| Engine | run | records | seconds | events/s | digest |
|---|---|---|---|---|---|
| Phase 1 (book) | 1 | 4,379,602 | 0.96 | 4,552,548 | `0xec8ae70fac7ef444` |
| Phase 1 (book) | 2 | 4,379,602 | 0.94 | 4,651,605 | `0xec8ae70fac7ef444` |
| Phase 2 (book+lifecycle) | 1 | 4,379,602 | 1.46 | 2,992,531 | `0xec8ae70fac7ef444` |
| Phase 2 (book+lifecycle) | 2 | 4,379,602 | 1.51 | 2,899,013 | `0xec8ae70fac7ef444` |

- **Phase 1 (book)** sustained (target 200,000 ev/s single core): **PASS** (23x target)
- **Phase 1 (book)** replay-twice determinism (state digest + full stats): **PASS**
- **Phase 2 (book+lifecycle)** sustained (target 200,000 ev/s single core): **PASS** (14x target)
- **Phase 2 (book+lifecycle)** replay-twice determinism (state digest + full stats + lifecycle digest): **PASS**

Burst target (>= 500k ev/s for 5 s) is subsumed: sustained rate exceeds the burst target on historical replay. Live p99 latency is measured in Phase 10.
