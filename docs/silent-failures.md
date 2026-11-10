# Silent failures: mechanism and evidence

These tests use real PostgreSQL. The subprocess chaos test is additional evidence, not a substitute for controlling the specific races.

| Failure | Mechanism | Concrete check |
| --- | --- | --- |
| Double claim | `FOR UPDATE SKIP LOCKED` inside the claim transaction | `test_simultaneous_claims_never_share_a_job`: 32 contenders, one winner; `test_skip_locked_does_not_wait_for_busy_job`: a locked earlier job does not block another claim |
| Zombie publishes stale success/failure | Owner + monotonic fence + current running state + unexpired lease | `test_expired_zombie_cannot_renew_complete_or_fail`; `test_owner_and_fence_are_both_checked`; a real SIGSTOP/SIGCONT process in `test_real_frozen_worker_returns_as_fenced_zombie` |
| Lease expires while completion waits | Clock is read after acquiring the job lock | `test_completion_after_lease_expires_while_waiting_for_lock` holds the lock and expires the lease before releasing it |
| Cancel/complete race | Both take the same row lock; first terminal transition wins | `test_cancel_complete_race_is_linearizable`: 40 concurrent races; tests also cover queued/running/idempotent cancellation and 409 after completion |
| Duplicate on request retry | Unique idempotency key and canonical payload fingerprint | 32 concurrent repository submissions, 24 concurrent HTTP submissions, conflicting payload reuse, and replay after completion |
| Effect committed without success, or vice versa | Single database transaction | `test_effect_and_success_roll_back_together`: injected exception after effect insertion; zero effects remain and retry can succeed |
| Lost commit response | Terminal row cannot be claimed/completed twice | `test_lost_completion_receipt_cannot_duplicate_effect`: repeat completion and POST after an acknowledged commit; effect count stays one. Actual transport loss during COMMIT is not injected |
| Isolation assumptions change | Explicit `READ COMMITTED`; separate statement after a conflicting insert | The clock/isolation test executes `SHOW transaction_isolation`; concurrent create tests exercise the conflict visibility path |
| Worker clock skew | All lease timestamps come from PostgreSQL | `test_worker_wall_clock_skew_cannot_change_lease`: worker-side datetime shifted a year in either direction; deadline stays within the DB-time lease interval. This is dependency-level clock injection, not host NTP manipulation |
| Lease revived after expiry | Renewal checks expiry and the token after locking | Expired heartbeat rejection, active heartbeat extension, and reclaim tests |
| Redis unavailable | Durable DB queue + bounded notification attempt + periodic polling | `test_redis_outage_does_not_lose_committed_jobs` uses a refused Redis endpoint, creates a job, reports degraded health, and completes it through the repository |
| Permanently crashing job stays running | Expired final attempt becomes failed | `test_expired_attempt_budget_fails_without_effect` exhausts a two-attempt test limit and checks zero effects |

The chaos audit checks every manifest key, API ID, first-creation count, stored payload hash, final state, effect cardinality and content, effect fence, contiguous attempt sequence, ownership-interval ordering, acknowledged cancellations, and terminal observations. It then GETs **every job** and compares the API with the database. Every recorded attempt must be closed; every job must be terminal.

An attempt count greater than one is expected after a crash. It is not a duplicate committed effect. The audit makes that distinction explicit and does not hide retries behind a uniqueness constraint.
