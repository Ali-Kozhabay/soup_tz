# Verdict: DON'T SHIP to production at 100 requests/second yet

The queue protocol passed the strengthened crash tests, and eight workers sustained the tested **100 create requests/second** workload locally. That establishes a useful prototype, not production readiness. One unexplained HTTP read failure remains in the evidence, and database failover, sustained resource pressure, admission limits, and deployment behavior have not been qualified.

## What actually ran

On 2026-11-10: native macOS ARM64, Python 3.12.13, PostgreSQL 17.6, Redis 8.2.2, one API process, and real worker processes. Dependency versions are locked in `uv.lock`; every experiment saves its exact source. These were **native process tests**, not container measurements. Compose YAML was validated with Compose 2.39.2. The local Docker daemon was stopped, so Docker image build/start and the supplied GitHub Actions workflow were **not executed** here. Compose targets PostgreSQL 17 and Redis 7.4; that Redis version difference is another unverified deployment detail.

There were **five chaos invocations, three load invocations, and one recovery invocation**. Every directory is retained in the [complete run ledger](../results/README.md). Four chaos runs completed 10,000 jobs; the first had weaker fault-coverage criteria. One chaos run aborted after 808 committed jobs. One load invocation failed before submission because the execution sandbox denied database access. Both failures remain failures. The last chaos run verifies the requested restructuring into `src/main.py`, feature routers, application services, core contracts, and infrastructure adapters.

| Strengthened chaos run | Jobs | SIGKILLs | Reclaimed attempts | Success / failure / cancellation | Violations |
| --- | ---: | ---: | ---: | --- | ---: |
| [Seed 20261112](../results/20261110T063739-chaos-5a29c442/summary.json) | 10,000 | 33 | 31 | 8,700 / 240 / 1,060 | 0 |
| [Seed 20261113](../results/20261110T064858-chaos-b24c83fd/summary.json) | 10,000 | 34 | 31 | 8,715 / 237 / 1,048 | 0 |
| [Seed 20261114, after restructure](../results/20261110T103730-chaos-1baf394c/summary.json) | 10,000 | 36 | 30 | 8,712 / 245 / 1,043 | 0 |

Each succeeded job had one effect; failed and cancelled jobs had zero. Every job was terminal, all attempts were closed, and every final API GET matched the database. The runs also made 2,768, 2,730, and 2,681 duplicate POSTs respectively. Median kill spacing in the first two was 2.009 and 2.008 seconds. Expected training failures are deliberately requested by the input manifest; they are not harness failures. All three strengthened runs had zero transport errors, so their success did not depend on the HTTP retry path.

## Capacity: API latency alone gave the wrong impression

Each load run offered 6,000 creates over approximately 60 seconds, with a deterministic 20–80 ms computation delay. There were no kills, cancellations, simulated training failures, or transport retries in these runs. All 6,000 jobs produced exactly one effect in each run.

| Workers | POST p95 / p99 | Scheduled-to-response p99 | Maximum sampled queue | Job completion p95 / p99 |
| --- | --- | --- | ---: | --- |
| [4](../results/20261110T065304-load-49b143b7/summary.json) | 3.445 / 4.583 ms | 5.606 ms | 1,876 | 28.120 / 29.522 s |
| [8](../results/20261110T065450-load-301688c3/summary.json) | 3.967 / 6.704 ms | 7.760 ms | 1 | 0.083 / 0.149 s |

Sampling is once per second, so the queue maximum is a sampled maximum, not an exact peak. The load generator schedules requests independently of preceding responses and reports scheduler delay: p99 was under 1 ms in both runs. Scheduled-to-response latency includes that delay. End-to-end job latency comes from database creation/finish timestamps. Final audits and draining are excluded from the 60-second offered-load interval.

**The four-worker default was changed to eight based on this result.** Four workers are not sustainable for this particular 100-new-jobs/sec workload, despite very fast HTTP responses. Eight is supported only for this small local simulation; minutes-long real training jobs require different capacity planning and backpressure. This is not a claim about every possible mixture of 100 API requests/sec.

## Failures and what changed

1. **False reassurance from fault coverage.** The first chaos run passed after 32 kills, but zero leases expired: its oldest-running-job canceller disposed of dead workers' jobs. Its result is unchanged and is not counted as proof of crash recovery. The harness now protects observed crash victims, randomizes live cancellation targets, and requires at least one expired attempt in a full run.
2. **Unexplained HTTP read failure.** The next run aborted with `httpx.ReadError`. It also showed delayed process startup/kill intervals and Redis publish timeouts. Those are symptoms, not a proven root cause. No API traceback establishes the cause. A separate recovery invocation drained the remaining 27 jobs among 808 persisted jobs, ending with 783 successes/effects, 21 expected failures, and four cancellations. The original failed verdict was not changed. Chaos HTTP transport errors now get up to three logged attempts; a lost creation receipt is treated as ambiguous, not as proof the database failed to commit. Producer tasks use structured cancellation. Load mode still fails on any transport error.
3. **Sandbox startup failure.** An initial load invocation could not connect to PostgreSQL (`PermissionError`). It submitted zero jobs. Its raw migration traceback is retained; the following authorized invocation ran normally.

## First release work

1. Add admission/backlog limits and queue-age/lease-recovery alerts; define a latency SLO for the actual training duration distribution. Keep measured worker headroom and test hours of sustained load and 10x bursts as history grows. Diagnose the unexplained connection failure with resource and network telemetry.
2. Verify the container build/start path and target dependency versions, exercise PostgreSQL restart/failover and backup restoration, and define acceptable durability on failover. Worker SIGKILL does not test database availability or replication loss.
3. Add authentication, tenant-scoped idempotency, quotas, and request-size limits before exposing the service outside a trusted environment. Define retention of jobs, effects, and idempotency keys together.
4. Before replacing the simulator, require idempotency/fencing at the real training/artifact destination. Database-row exactly-once effects do not imply exactly-once external work.

## What the harness does not catch

Finite random schedules cannot prove correctness for every interleaving. The audit sees durable rows and request observations, not every CPU execution. It does not cover power loss, disk corruption, database primary failover, long network partitions, clock jumps on the database host, isolation levels other than the configured one, malicious/direct SQL writers, external side effects, or long-lived memory/connection leaks. It does not deliberately sever the network during COMMIT. The targeted frozen-worker test pauses for seconds, not five minutes. Clock-skew injection replaces a worker dependency rather than changing host NTP. The API and load generator share one machine, and local Redis-outage tests do not reproduce a multi-host Redis partition.

The latest integration suite reports [22 passing tests](../results/refactor-verification-pytest.log). Its in-process coverage is 75%; independently launched worker processes are exercised by the process tests and chaos harness but are not included in that coverage trace. Lint, formatting, strict type checks, wheel/sdist packaging, and Compose configuration validation passed. The rebuilt wheel contains `src.main:app` and the new worker entry point; installed modules import from outside the repository. Alembic found no new upgrade operations, and the [OpenAPI contract hashes](../results/refactor-openapi-comparison.json) match before and after the restructure. The earlier load measurements remain historical measurements of the pre-refactor layout. These are distinct checks, not substitutes for the failure evidence above.
