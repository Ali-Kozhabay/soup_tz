# Complete experiment ledger

All nine invocations are retained. Original logs and summaries have not been rewritten. Times in directory names and JSON events are UTC. A completed invariant audit is distinct from sufficient production evidence.

| # | Directory | Purpose | Outcome |
| --- | --- | --- | --- |
| 1 | [20261110T062718-chaos-59d71e78](20261110T062718-chaos-59d71e78/summary.json) | 10,000 jobs, 8 workers, seed 20261110 | Passed original checks; 32 kills, **zero recoveries**. Weak evidence; cancellation masked recovery. |
| 2 | [20261110T063134-chaos-0aab894b](20261110T063134-chaos-0aab894b/summary.json) | Planned 10,000 jobs, 8 workers, seed 20261111 | **Failed** with `httpx.ReadError`; 808 jobs persisted, 12 kills before abort. No final full audit. |
| 3 | [20261110T063535-recovery-91075c6a](20261110T063535-recovery-91075c6a/summary.json) | Drain run 2's persisted jobs without resubmitting its uncreated manifest entries | Recovered remaining 27 of 808 jobs. Separate before/after database snapshots; run 2 remains failed. |
| 4 | [20261110T063739-chaos-5a29c442](20261110T063739-chaos-5a29c442/summary.json) | 10,000 jobs, 8 workers, seed 20261112 | Passed strengthened checks: 33 kills, 31 expired attempts recovered, zero violations. |
| 5 | [20261110T064858-chaos-b24c83fd](20261110T064858-chaos-b24c83fd/summary.json) | 10,000 jobs, 8 workers, seed 20261113 | Passed strengthened checks: 34 kills, 31 expired attempts recovered, zero violations. |
| 6 | [20261110T065037-load-362c42bf](20261110T065037-load-362c42bf/summary.json) | Initial 100 creates/sec load invocation | **Failed before submission**: sandbox denied the migration's DB connection. Zero jobs created. |
| 7 | [20261110T065304-load-49b143b7](20261110T065304-load-49b143b7/summary.json) | 6,000 jobs, 100 creates/sec for 60 seconds, 4 workers | Correctness passed; capacity insufficient: maximum sampled queue 1,876, completion p95 28.120 s. |
| 8 | [20261110T065450-load-301688c3](20261110T065450-load-301688c3/summary.json) | 6,000 jobs, 100 creates/sec for 60 seconds, 8 workers | Passed: maximum sampled queue 1, completion p95 0.083 s; zero transport errors. |
| 9 | [20261110T103730-chaos-1baf394c](20261110T103730-chaos-1baf394c/summary.json) | After the requested architecture restructure: 10,000 jobs, 8 workers, seed 20261114 | Passed: 36 kills, 30 expired attempts recovered, zero violations or transport errors. |

Each full experiment saves:

- `manifest.jsonl`: intended input keys and normalized payloads.
- `requests.jsonl`: actual HTTP status and response bodies, including repeated requests and final reads.
- `events.jsonl`: raw harness output, fault timing, progress, and errors.
- `api.log`, `worker-SLOT-GENERATION.log`, `migration.log`: original child-process output.
- `source.tar.gz`, `source-hashes.json`: the exact code used in that invocation.
- `database-audit.json.gz`: final persisted rows, when the experiment reached the audit.
- `summary.json`: original outcome and measured counters.

Recovery uses `before.json.gz` and `after.json.gz`. Failed invocations can lack a final audit because execution stopped earlier. The [verdict](../docs/verdict.md) explains all failures and the limits of the measurements. [verification-pytest.log](verification-pytest.log) retains the original test output. [refactor-verification-pytest.log](refactor-verification-pytest.log) records the subsequent 22 passing tests against the restructured code; [refactor-openapi-comparison.json](refactor-openapi-comparison.json) records identical before/after API contract hashes. Original run source snapshots retain their original package paths.
