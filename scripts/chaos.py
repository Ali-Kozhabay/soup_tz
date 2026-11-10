"""Real HTTP + PostgreSQL + OS-process fault injection. Every invocation keeps its evidence."""

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import platform
import random
import signal
import socket
import sys
import tarfile
import time
import traceback
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.config import Settings
from src.infrastructure.database.session import make_engine

ROOT = Path(__file__).resolve().parents[1]


def dump(value):
    return json.dumps(value, default=str, sort_keys=True)


def percentile(values, percent):
    ordered = sorted(values)
    return (
        round(ordered[min(len(ordered) - 1, int(len(ordered) * percent))], 3) if ordered else None
    )


class Evidence:
    def __init__(self, args):
        self.run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{args.mode}-{uuid4().hex[:8]}"
        self.path = ROOT / "results" / self.run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.events = (self.path / "events.jsonl").open("x", buffering=1)
        self.requests = (self.path / "requests.jsonl").open("x", buffering=1)
        self.emit("start", arguments=vars(args), python=sys.version, platform=platform.platform())
        files = sorted(
            {
                *ROOT.glob("src/**/*.py"),
                *ROOT.glob("scripts/*.py"),
                *ROOT.glob("tests/*.py"),
                *ROOT.glob("migrations/**/*.py"),
                ROOT / "pyproject.toml",
                ROOT / "uv.lock",
                ROOT / "compose.yaml",
                ROOT / "Dockerfile",
                ROOT / "alembic.ini",
            }
        )
        hashes = {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
        }
        (self.path / "source-hashes.json").write_text(dump(hashes) + "\n")
        with tarfile.open(self.path / "source.tar.gz", "w:gz") as archive:
            for file in files:
                archive.add(file, arcname=str(file.relative_to(ROOT)))

    def emit(self, event, **data):
        line = dump({"time": datetime.now(UTC), "event": event, **data})
        self.events.write(line + "\n")
        print(line, flush=True)


class Experiment:
    def __init__(self, args, evidence):
        self.args, self.evidence = args, evidence
        settings = Settings()
        url = make_url(settings.database_url)
        # Always isolated from the normal application and integration-test databases.
        self.settings = settings.model_copy(
            update={
                "database_url": url.set(database="soup_chaos").render_as_string(hide_password=False)
            }
        )
        self.engine = make_engine(self.settings)
        self.env = {
            **os.environ,
            "SOUP_DATABASE_URL": self.settings.database_url,
            "SOUP_REDIS_URL": self.settings.redis_url,
            "SOUP_REDIS_CHANNEL": f"soup:{evidence.run_id}",
            "SOUP_LEASE_SECONDS": "2",
            "SOUP_HEARTBEAT_SECONDS": "0.4",
            "SOUP_POLL_SECONDS": "0.1",
        }
        self.processes = []
        self.handles = []
        self.workers = {}
        self.generations = Counter()
        self.stop = asyncio.Event()
        self.prefix = evidence.run_id + ":"
        self.ids = {}
        self.cancelled = set()
        self.observed_terminal = {}
        self.creations = Counter()
        self.operations = Counter()
        self.latencies = []
        self.scheduled_latencies = []
        self.scheduling_lag = []
        self.kills = 0
        self.killed_jobs = set()
        self.transport_errors = 0
        self.ambiguous_creations = set()
        self.max_queued = 0
        self.manifest = []
        rng = random.Random(args.seed)
        count = args.jobs if args.mode == "chaos" else int(args.rps * args.seconds)
        for index in range(count):
            self.manifest.append(
                {
                    "index": index,
                    "key": f"{self.prefix}{index}",
                    "payload": {
                        "model": "tiny-transformer",
                        "dataset": "synthetic-v1",
                        "epochs": 3,
                        "seed": index,
                        "duration_ms": rng.randint(20, 80),
                        "simulate_failure": args.mode == "chaos" and index % 37 == 0,
                    },
                }
            )
        with (evidence.path / "manifest.jsonl").open("x") as output:
            for item in self.manifest:
                output.write(dump(item) + "\n")

    async def spawn(self, name, *command):
        output = (self.evidence.path / f"{name}.log").open("x")
        self.handles.append(output)
        process = await asyncio.create_subprocess_exec(
            *command, cwd=ROOT, env=self.env, stdout=output, stderr=asyncio.subprocess.STDOUT
        )
        self.processes.append(process)
        self.evidence.emit("process_started", name=name, pid=process.pid)
        return process

    async def start_worker(self, slot):
        self.generations[slot] += 1
        self.workers[slot] = await self.spawn(
            f"worker-{slot}-{self.generations[slot]}", sys.executable, "-m", "src.worker"
        )

    async def setup(self):
        migration = await self.spawn(
            "migration", sys.executable, "-m", "alembic", "upgrade", "head"
        )
        if await migration.wait() != 0:
            raise RuntimeError("Migration failed; see migration.log")
        async with self.engine.connect() as connection:
            metadata = (
                (
                    await connection.execute(
                        text(
                            "SELECT version(), "
                            "current_setting('transaction_isolation') AS isolation"
                        )
                    )
                )
                .mappings()
                .one()
            )
            self.evidence.emit("database", **dict(metadata))
            remaining = await connection.scalar(
                text("SELECT count(*) FROM jobs WHERE status IN ('queued','running')")
            )
            if remaining:
                raise RuntimeError(
                    "Dedicated chaos DB contains unfinished jobs from an earlier run; "
                    "inspect them before rerunning (no automatic deletion)"
                )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.api = await self.spawn(
            "api",
            sys.executable,
            "-m",
            "uvicorn",
            "src.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        )
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout=20,
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=64),
        )
        async with asyncio.timeout(30):
            while True:
                try:
                    response = await self.client.get("/health/ready")
                    if response.status_code == 200:
                        self.evidence.emit("api_ready", health=response.json())
                        break
                except httpx.TransportError:
                    pass
                if self.api.returncode is not None:
                    raise RuntimeError("API exited during startup")
                await asyncio.sleep(0.1)
        for slot in range(self.args.workers):
            await self.start_worker(slot)

    async def request(self, method, path, *, index=None, **kwargs):
        for attempt in range(3):
            try:
                return await self.client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                self.transport_errors += 1
                if method == "POST" and path == "/v1/jobs":
                    self.ambiguous_creations.add(index)
                self.evidence.emit(
                    "transport_error",
                    method=method,
                    path=path,
                    index=index,
                    attempt=attempt + 1,
                    error=repr(exc),
                )
                if self.args.mode == "load" or attempt == 2:
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))
        raise AssertionError("Unreachable retry state")

    async def post(self, index, operation="create", scheduled=None):
        item = self.manifest[index]
        start = time.perf_counter()
        if scheduled is not None:
            self.scheduling_lag.append((start - scheduled) * 1000)
        response = await self.request(
            "POST",
            "/v1/jobs",
            index=index,
            json=item["payload"],
            headers={"Idempotency-Key": item["key"]},
        )
        elapsed = (time.perf_counter() - start) * 1000
        self.evidence.requests.write(
            dump(
                {
                    "operation": operation,
                    "index": index,
                    "http_status": response.status_code,
                    "duration_ms": elapsed,
                    "body": response.json(),
                }
            )
            + "\n"
        )
        if response.status_code not in {200, 201}:
            raise AssertionError(f"Unexpected POST status {response.status_code}: {response.text}")
        body = response.json()
        if index in self.ids and self.ids[index] != body["id"]:
            raise AssertionError(f"Idempotency violation at index {index}")
        self.ids[index] = body["id"]
        self.creations[index] += response.status_code == 201
        self.operations[operation] += 1
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            self.remember_terminal(body)
        if operation == "create":
            self.latencies.append(elapsed)
            if scheduled is not None:
                self.scheduled_latencies.append((time.perf_counter() - scheduled) * 1000)

    def remember_terminal(self, body):
        previous = self.observed_terminal.setdefault(body["id"], body["status"])
        if previous != body["status"]:
            raise AssertionError(f"Terminal state changed: {previous} -> {body['status']}")

    async def cancel(self, job_id):
        if job_id in self.killed_jobs:
            return  # Preserve crash victims so cancellation cannot hide lease recovery.
        response = await self.request("POST", f"/v1/jobs/{job_id}/cancel")
        self.evidence.requests.write(
            dump(
                {
                    "operation": "cancel",
                    "job_id": job_id,
                    "http_status": response.status_code,
                    "body": response.json(),
                }
            )
            + "\n"
        )
        if response.status_code == 200:
            assert response.json()["status"] == "cancelled"
            self.cancelled.add(job_id)
            self.remember_terminal(response.json())
        elif response.status_code != 409:
            raise AssertionError(f"Unexpected cancellation status {response.status_code}")
        self.operations[f"cancel_{response.status_code}"] += 1

    async def produce(self):
        async def producer(offset):
            for index in range(offset, len(self.manifest), 32):
                await self.post(index)
                if index % 11 == 0:
                    await self.cancel(self.ids[index])

        async with asyncio.TaskGroup() as group:
            for offset in range(32):
                group.create_task(producer(offset))
        self.evidence.emit("submission_complete", unique_jobs=len(self.ids))

    async def repeat_posts(self):
        rng = random.Random(self.args.seed + 10)
        while not self.stop.is_set():
            # Include not-yet-acknowledged keys: this also races first creation.
            await self.post(rng.randrange(len(self.manifest)), "duplicate")
            await asyncio.sleep(0.01)

    async def cancel_running(self):
        while not self.stop.is_set():
            async with self.engine.connect() as connection:
                job_id = await connection.scalar(
                    text(
                        "SELECT id FROM jobs WHERE idempotency_key LIKE :prefix "
                        "AND status='running' AND lease_until > clock_timestamp() "
                        "ORDER BY random() LIMIT 1"
                    ),
                    {"prefix": self.prefix + "%"},
                )
            if job_id:
                await self.cancel(str(job_id))
                self.operations["targeted_running_cancel"] += 1
            await asyncio.sleep(0.2)

    async def kill_workers(self):
        rng = random.Random(self.args.seed + 20)
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), self.args.kill_interval)
                return
            except TimeoutError:
                pass
            slot = rng.randrange(self.args.workers)
            worker = self.workers[slot]
            if worker.returncode is not None:
                raise RuntimeError(f"Worker {slot} exited unexpectedly with {worker.returncode}")
            worker.send_signal(signal.SIGKILL)
            await worker.wait()
            async with self.engine.connect() as connection:
                victims = (
                    (
                        await connection.execute(
                            text(
                                "SELECT id FROM jobs WHERE status='running' "
                                "AND lease_owner LIKE :owner"
                            ),
                            {"owner": f"{worker.pid}-%"},
                        )
                    )
                    .scalars()
                    .all()
                )
            self.killed_jobs.update(str(job_id) for job_id in victims)
            self.kills += 1
            self.evidence.emit(
                "sigkill",
                slot=slot,
                pid=worker.pid,
                returncode=worker.returncode,
                abandoned_jobs=[str(job_id) for job_id in victims],
            )
            await self.start_worker(slot)

    async def counts(self):
        async with self.engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT status, count(*) FROM jobs "
                    "WHERE idempotency_key LIKE :prefix GROUP BY status"
                ),
                {"prefix": self.prefix + "%"},
            )
            return dict(result.all())

    async def drain(self):
        async with asyncio.timeout(self.args.drain_timeout):
            while True:
                states = await self.counts()
                self.evidence.emit("progress", states=states, kills=self.kills)
                for slot, process in self.workers.items():
                    # A deliberately killed worker may be between wait() and replacement.
                    if process.returncode not in (None, -signal.SIGKILL):
                        raise RuntimeError(
                            f"Worker {slot} exited unexpectedly: {process.returncode}"
                        )
                if sum(states.values()) == len(self.manifest) and not (
                    states.get("queued", 0) + states.get("running", 0)
                ):
                    return
                await asyncio.sleep(1)

    async def load(self):
        start = time.perf_counter()
        tasks = []
        sampling_done = asyncio.Event()

        async def sample_backlog():
            while not sampling_done.is_set():
                states = await self.counts()
                self.max_queued = max(self.max_queued, states.get("queued", 0))
                self.evidence.emit(
                    "load_sample", elapsed=time.perf_counter() - start, states=states
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(sampling_done.wait(), 1)

        async with asyncio.TaskGroup() as group:
            sampler = group.create_task(sample_backlog())
            try:
                for index in range(len(self.manifest)):
                    scheduled = start + index / self.args.rps
                    await asyncio.sleep(max(0, scheduled - time.perf_counter()))
                    tasks.append(group.create_task(self.post(index, scheduled=scheduled)))
                await asyncio.gather(*tasks)
            finally:
                sampling_done.set()
                await sampler
        self.evidence.emit("load_submitted", count=len(tasks), elapsed=time.perf_counter() - start)
        await self.drain()

    async def audit(self):
        async with self.engine.connect() as connection:
            data = {}
            for table in ("jobs", "job_attempts", "job_effects"):
                query = (
                    "SELECT * FROM jobs WHERE idempotency_key LIKE :prefix"
                    if table == "jobs"
                    else f"SELECT t.* FROM {table} t JOIN jobs j ON j.id=t.job_id "
                    "WHERE j.idempotency_key LIKE :prefix"
                )
                data[table] = [
                    dict(row)
                    for row in (
                        await connection.execute(text(query), {"prefix": self.prefix + "%"})
                    ).mappings()
                ]
        with gzip.open(self.evidence.path / "database-audit.json.gz", "wt") as output:
            output.write(dump(data))
        jobs = {job["idempotency_key"]: job for job in data["jobs"]}
        effects = {}
        attempts = {}
        for effect in data["job_effects"]:
            effects.setdefault(effect["job_id"], []).append(effect)
        for attempt in data["job_attempts"]:
            attempts.setdefault(attempt["job_id"], []).append(attempt)
        violations = []

        def check(condition, detail):
            if not condition:
                violations.append(detail)

        check(len(jobs) == len(self.manifest), "missing or extra persisted jobs")
        check(len(set(self.ids.values())) == len(self.manifest), "missing or reused API IDs")
        for item in self.manifest:
            job = jobs.get(item["key"])
            if not job:
                violations.append(f"missing {item['key']}")
                continue
            label = str(job["id"])
            payload_hash = hashlib.sha256(
                json.dumps(item["payload"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            check(str(job["id"]) == self.ids.get(item["index"]), f"ID mismatch {label}")
            receipts = self.creations[item["index"]]
            check(
                receipts == 1 or (receipts == 0 and item["index"] in self.ambiguous_creations),
                f"incorrect first-creation count {label}",
            )
            check(
                job["payload"] == item["payload"] and job["payload_hash"] == payload_hash,
                f"payload mismatch {label}",
            )
            check(job["status"] in {"succeeded", "failed", "cancelled"}, f"nonterminal {label}")
            check(
                job["finished_at"] is not None
                and job["lease_until"] is None
                and job["lease_owner"] is None,
                f"terminal metadata inconsistent {label}",
            )
            check(
                label not in self.cancelled or job["status"] == "cancelled",
                f"acknowledged cancellation reversed {label}",
            )
            check(
                label not in self.observed_terminal
                or job["status"] == self.observed_terminal[label],
                f"terminal state reversed {label}",
            )
            job_effects = effects.get(job["id"], [])
            check(
                len(job_effects) == int(job["status"] == "succeeded"), f"effect cardinality {label}"
            )
            for effect in job_effects:
                check(
                    effect["fence"] == job["fence"]
                    and effect["result"]
                    == {
                        "artifact_sha256": payload_hash,
                        "epochs": item["payload"]["epochs"],
                        "seed": item["payload"]["seed"],
                    },
                    f"stale or incorrect effect {label}",
                )
            if job["status"] == "succeeded":
                check(
                    not item["payload"]["simulate_failure"] and job["error"] is None,
                    f"false success {label}",
                )
            if job["status"] == "failed":
                check(
                    (
                        item["payload"]["simulate_failure"]
                        and job["error"] == "simulated_training_failure"
                    )
                    or (job["error"] == "attempt_limit_exceeded" and job["fence"] >= 25),
                    f"unexplained failure {label}",
                )
            history = sorted(attempts.get(job["id"], []), key=lambda attempt: attempt["fence"])
            check(
                [a["fence"] for a in history] == list(range(1, job["fence"] + 1)),
                f"missing or duplicate claim history {label}",
            )
            for position, attempt in enumerate(history):
                check(
                    attempt["ended_at"] is not None and attempt["outcome"] != "running",
                    f"unfinished attempt {label}",
                )
                if attempt["ended_at"]:
                    check(attempt["claimed_at"] <= attempt["ended_at"], f"time reversal {label}")
                if position < len(history) - 1:
                    check(
                        attempt["outcome"] == "expired"
                        and attempt["ended_at"] <= history[position + 1]["claimed_at"],
                        f"overlapping claim ownership {label}",
                    )
            if history:
                expected = "expired" if job["error"] == "attempt_limit_exceeded" else job["status"]
                check(history[-1]["outcome"] == expected, f"attempt/final-state mismatch {label}")

        async def read_final(offset):
            for item in self.manifest[offset::32]:
                job_id = self.ids.get(item["index"])
                response = await self.request("GET", f"/v1/jobs/{job_id}")
                body = response.json()
                self.evidence.requests.write(
                    dump(
                        {
                            "operation": "final_read",
                            "job_id": job_id,
                            "http_status": response.status_code,
                            "body": body,
                        }
                    )
                    + "\n"
                )
                check(
                    response.status_code == 200
                    and body.get("status") == jobs.get(item["key"], {}).get("status"),
                    f"API/database mismatch {job_id}",
                )

        await asyncio.gather(*(read_final(offset) for offset in range(32)))
        if self.args.mode == "chaos" and len(self.manifest) >= 10_000:
            check(
                self.kills > 0
                and self.operations["duplicate"] > 0
                and self.operations["targeted_running_cancel"] > 0,
                "required faults did not occur",
            )
            check(
                any(a["outcome"] == "expired" for a in data["job_attempts"]),
                "no lease recovery observed; kills alone are insufficient evidence",
            )
        result = {
            "jobs": len(jobs),
            "states": dict(Counter(j["status"] for j in jobs.values())),
            "effects": len(data["job_effects"]),
            "attempts": len(data["job_attempts"]),
            "expired_attempts": sum(a["outcome"] == "expired" for a in data["job_attempts"]),
            "workers": self.args.workers,
            "worker_processes_started": sum(self.generations.values()),
            "sigkills": self.kills,
            "transport_errors": self.transport_errors,
            "ambiguous_creation_receipts": len(self.ambiguous_creations),
            "max_sampled_queued_during_load": self.max_queued,
            "operations": dict(self.operations),
            "final_reads": len(self.manifest),
            "post_latency_ms": {
                "p50": percentile(self.latencies, 0.5),
                "p95": percentile(self.latencies, 0.95),
                "p99": percentile(self.latencies, 0.99),
            },
            "scheduled_to_response_ms": {
                "p95": percentile(self.scheduled_latencies, 0.95),
                "p99": percentile(self.scheduled_latencies, 0.99),
            },
            "scheduler_lag_ms_p99": percentile(self.scheduling_lag, 0.99),
            "violations": violations,
        }
        durations = [
            (job["finished_at"] - job["created_at"]).total_seconds()
            for job in jobs.values()
            if job["finished_at"]
        ]
        result["job_completion_seconds"] = {
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
        }
        self.evidence.emit("audit", **result)
        if violations:
            raise AssertionError(f"{len(violations)} invariant violations; see events.jsonl")
        return result

    async def run(self):
        await self.setup()
        if self.args.mode == "load":
            await self.load()
        else:
            async with asyncio.TaskGroup() as group:
                group.create_task(self.kill_workers())
                group.create_task(self.repeat_posts())
                group.create_task(self.cancel_running())
                try:
                    await self.produce()
                    await self.drain()
                finally:
                    self.stop.set()
        return await self.audit()

    async def close(self):
        self.stop.set()
        for process in self.processes:
            if process.returncode is None:
                process.terminate()
        for process in self.processes:
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if hasattr(self, "client"):
            await self.client.aclose()
        await self.engine.dispose()
        for handle in self.handles:
            handle.close()


async def main(args):
    evidence = Evidence(args)
    experiment = Experiment(args, evidence)
    start = time.perf_counter()
    summary = {"run_id": evidence.run_id, "arguments": vars(args), "passed": False}
    try:
        summary.update(await experiment.run())
        summary["passed"] = True
    except BaseException as exc:
        summary["error"] = repr(exc)
        evidence.emit("failed", error=repr(exc), traceback=traceback.format_exc())
    finally:
        try:
            await experiment.close()
        except BaseException as exc:
            summary["passed"] = False
            summary["cleanup_error"] = repr(exc)
            evidence.emit("cleanup_failed", traceback=traceback.format_exc())
        summary["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        (evidence.path / "summary.json").write_text(dump(summary) + "\n")
        evidence.emit("finished", **summary)
        evidence.requests.close()
        evidence.events.close()
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["chaos", "load"], default="chaos")
    parser.add_argument("--jobs", type=int, default=10_000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20261110)
    parser.add_argument("--kill-interval", type=float, default=2)
    parser.add_argument("--drain-timeout", type=float, default=300)
    parser.add_argument("--rps", type=float, default=100)
    parser.add_argument("--seconds", type=float, default=60)
    arguments = parser.parse_args()
    if (
        min(
            arguments.jobs,
            arguments.workers,
            arguments.rps,
            arguments.seconds,
            arguments.kill_interval,
            arguments.drain_timeout,
        )
        <= 0
    ):
        parser.error("All numeric arguments must be positive")
    sys.exit(asyncio.run(main(arguments)))
