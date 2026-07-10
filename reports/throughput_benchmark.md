# Throughput + determinism benchmark (Phase 0 CI)

Generated: 2026-07-10T10:59:53+00:00
Session: 2026-01-06  |  file records: 4,379,602

| Engine | run | records | seconds | events/s | digest |
|---|---|---|---|---|---|
| Phase 1 (book) | 1 | 4,379,602 | 0.95 | 4,612,302 | `0xec8ae70fac7ef444` |
| Phase 1 (book) | 2 | 4,379,602 | 0.95 | 4,619,616 | `0xec8ae70fac7ef444` |
| Phase 2 (book+lifecycle) | 1 | 4,379,602 | 1.50 | 2,922,019 | `0xec8ae70fac7ef444` |
| Phase 2 (book+lifecycle) | 2 | 4,379,602 | 1.54 | 2,834,896 | `0xec8ae70fac7ef444` |
| Phase 3 (book+lifecycle+flow) | 1 | 4,379,602 | 3.67 | 1,193,986 | `0xec8ae70fac7ef444` |
| Phase 3 (book+lifecycle+flow) | 2 | 4,379,602 | 3.67 | 1,194,445 | `0xec8ae70fac7ef444` |

- **Phase 1 (book)** sustained (target 200,000 ev/s single core): **PASS** (23x target)
- **Phase 1 (book)** replay-twice determinism (state digest + full stats): **PASS**
- **Phase 2 (book+lifecycle)** sustained (target 200,000 ev/s single core): **PASS** (14x target)
- **Phase 2 (book+lifecycle)** replay-twice determinism (state digest + full stats + lifecycle digest): **PASS**
- **Phase 3 (book+lifecycle+flow)** sustained (target 200,000 ev/s single core): **PASS** (6x target)
- **Phase 3 (book+lifecycle+flow)** replay-twice determinism (state digest + full stats + lifecycle digest): **PASS**

Burst target (>= 500k ev/s for 5 s) is subsumed: sustained rate exceeds the burst target on historical replay. Live p99 latency is measured in Phase 10.
