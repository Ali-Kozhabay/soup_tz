# Soup Training Queue

An async FastAPI service for training jobs, with SQLAlchemy 2, PostgreSQL, and Redis. Built for the Soup Backend Engineer take-home: correctness under worker death, duplicate requests, and cancellation races.

**The guarantee:** execution attempts may repeat; a succeeded job has exactly one committed PostgreSQL side-effect row. Cancelled and failed jobs have none. External training APIs and file writes are outside this guarantee.

## Run

```sh
docker compose up --build
```

This starts PostgreSQL 17, Redis, a one-shot Alembic migration, the API, and **eight independent workers**. Open [Swagger UI](http://localhost:8000/docs). Readiness: `GET /health/ready`. Redis can be unavailable while the service remains usable through database polling. Ports are bound to localhost; the supplied credentials are for local demonstration.

Eight is an evidence-based default: the 100-create-requests/sec experiment showed backlog growth with four workers. See the [capacity comparison and verdict](docs/verdict.md).

## Project structure

The layout follows the feature-based web modules in the supplied [reference project](https://github.com/Asylniet/fastapi-demo-project/tree/main/src/api/web). `main.py` and `config.py` sit together at the source root.

```text
src/
├── main.py                         # FastAPI factory, lifespan, router registration
├── config.py                       # Environment settings
├── worker.py                       # Independent worker entry point
├── api/web/
│   ├── dependencies.py
│   ├── exceptions.py
│   ├── middleware.py
│   ├── jobs/{routers.py, schemas.py}
│   └── health/routers.py
├── application/services/jobs.py    # Job operations through core contracts
├── core/
│   ├── dto/jobs.py                 # Training specification, claim, job snapshot
│   ├── enums/jobs.py
│   ├── exceptions.py
│   ├── interfaces/repositories/jobs.py
│   └── interfaces/storage/notifications.py
└── infrastructure/
    ├── adapters/di/container.py
    ├── adapters/storage/redis.py
    ├── database/session.py
    ├── database/models/{base.py, jobs.py}
    └── repositories/{jobs.py, cursors.py}
```

The API entry point is `src.main:app`; launch it with `uv run uvicorn src.main:app --reload`. Business routes call the service, the service depends on core protocols, and the SQLAlchemy repository returns job snapshots across that boundary. Resource construction lives in the DI container.

```sh
curl -i http://localhost:8000/v1/jobs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: training-demo-1' \
  -d '{"model":"tiny-transformer","dataset":"synthetic-v1","epochs":3,"duration_ms":2000}'

curl http://localhost:8000/v1/jobs/JOB_ID
curl 'http://localhost:8000/v1/jobs?limit=10&status=queued'
curl -X POST http://localhost:8000/v1/jobs/JOB_ID/cancel
```

Retry the first POST with the same key and body: it returns the same ID. All payload fields have defaults; unknown fields and invalid types are rejected. `dataset` is a label, never a fetched URL. `duration_ms` (0–30,000) and `simulate_failure` expose deterministic failure scenarios for this exercise.

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/jobs` | Required `Idempotency-Key`; 201 on creation, 200 on replay, 409 on conflicting reuse |
| `GET /v1/jobs/{id}` | Status, payload, attempt fence, timestamps, and failure reason; 404 if absent |
| `GET /v1/jobs` | `limit` 1–100, optional `status`, opaque `cursor`; returns `items` and `next_cursor` |
| `POST /v1/jobs/{id}/cancel` | 200 for queued/running/already-cancelled; 409 after success/failure |
| `GET /health/live` | Process liveness |
| `GET /health/ready` | 503 if database/schema unavailable; 200/degraded if only Redis is down |

Pagination carries a first-page time ceiling. It avoids offset drift but is **not a snapshot**: concurrent changes to a status filter, or an older uncommitted insert becoming visible later, can change membership. Keep the same status filter when following a cursor. `fence` also counts claimed attempts. Keys are retained for the lifetime of their jobs.

## Read the reasoning and evidence

- [One-page design PDF](docs/design.pdf) and [design source written before implementation](docs/design.md): claim protocol, cancellation, recovery, and guarantee boundary.
- [Architecture and tradeoffs](docs/architecture.md): component responsibilities and transaction sequence.
- [Silent-failure checks](docs/silent-failures.md): each required failure mode mapped to a concrete test.
- [Evidence and production verdict](docs/verdict.md): all runs, measurements, blind spots, and release priorities.
- [AI disclosure and reflection](docs/ai-disclosure.md): what was generated and what was actually verified.
- [Raw evidence](results/): every harness invocation, including failures, with source snapshots.

## Verification

```sh
# Real PostgreSQL tests, including SIGSTOP/SIGCONT of a real worker.
docker compose --profile tools run --rm test

# Eight child worker processes; SIGKILL and restart about every two seconds.
docker compose --profile tools run --rm chaos

# A second seed; results always go into a NEW directory.
docker compose --profile tools run --rm chaos \
  python scripts/chaos.py --jobs 10000 --workers 8 --seed 20261111

# Open-loop offered load: 100 create requests/sec for 60 seconds, eight workers.
docker compose --profile tools run --rm chaos \
  python scripts/chaos.py --mode load --rps 100 --seconds 60 --workers 8
```

The harness runs its **own API and workers** against the dedicated `soup_chaos` database. It never deletes previous jobs or evidence. A previous unfinished run must be investigated before another invocation; startup refuses to silently discard it. Compose initializes the extra databases on a fresh volume. Existing volumes created without the supplied initialization script need `soup_test` and `soup_chaos` created explicitly.

Each run contains `events.jsonl`, every HTTP response in `requests.jsonl`, the input manifest, individual worker/API logs, the database audit, source hashes, a source tarball, and `summary.json`. A nonzero exit or `passed: false` is a failed run. The seed fixes input data and victim selection; OS scheduling is intentionally nondeterministic.

Aborted runs may have only partial HTTP evidence and no final database audit. `scripts/recover.py EXACT_RUN_ID` can drain their persisted jobs and save before/after snapshots in a **separate** evidence directory; it never upgrades the original failed verdict. Chaos transport retries are bounded to three attempts and logged. Load mode does not retry transport failures. The first full run used an earlier, weaker harness; the evidence report explains this openly.

To inspect the real effect count:

```sh
docker compose exec postgres psql -U soup -d soup -c \
  'SELECT j.status, count(*) AS jobs, count(e.job_id) AS effects
   FROM jobs j LEFT JOIN job_effects e ON e.job_id=j.id GROUP BY j.status;'
```

## Local development

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/). With PostgreSQL and Redis running:

```sh
cp .env.example .env
uv sync --locked
uv run alembic upgrade head
uv run uvicorn src.main:app --reload
# In separate terminals for each worker, or use Compose workers:
uv run soup-worker

uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Tests use a dedicated database ending in `_test` and truncate only its queue tables. Set `SOUP_TEST_DATABASE_URL` if needed. All runtime configuration uses the `SOUP_` prefix; see [.env.example](.env.example). PostgreSQL is required for concurrency tests; SQLite mocks would not exercise these locking semantics.

Stop the demonstration with `docker compose down`. The named PostgreSQL volume retains jobs.
