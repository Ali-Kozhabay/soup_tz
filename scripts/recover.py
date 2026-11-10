"""Drain persisted jobs after an aborted run without changing or deleting its evidence."""

import argparse
import asyncio
import gzip
import json
import os
import re
import sys
import time

from chaos import Evidence
from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.config import Settings
from src.infrastructure.database.session import make_engine


async def main(args):
    evidence = Evidence(args)
    settings = Settings()
    url = make_url(settings.database_url).set(database="soup_chaos")
    config = settings.model_copy(update={"database_url": url.render_as_string(hide_password=False)})
    engine = make_engine(config)
    processes, handles = [], []
    start = time.perf_counter()
    passed = False

    async def snapshot(label):
        data = {}
        async with engine.connect() as connection:
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
                        await connection.execute(text(query), {"prefix": args.run_id + ":%"})
                    ).mappings()
                ]
        with gzip.open(evidence.path / f"{label}.json.gz", "wt") as output:
            json.dump(data, output, default=str)
        return data

    try:
        before = await snapshot("before")
        evidence.emit("before", persisted_jobs=len(before["jobs"]), original_run=args.run_id)
        env = {
            **os.environ,
            "SOUP_DATABASE_URL": config.database_url,
            "SOUP_REDIS_URL": config.redis_url,
        }
        for slot in range(4):
            handle = (evidence.path / f"worker-{slot}.log").open("x")
            handles.append(handle)
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "src.worker",
                    env=env,
                    stdout=handle,
                    stderr=asyncio.subprocess.STDOUT,
                )
            )
        async with asyncio.timeout(120):
            while True:
                async with engine.connect() as connection:
                    remaining = await connection.scalar(
                        text(
                            "SELECT count(*) FROM jobs WHERE idempotency_key LIKE :prefix "
                            "AND status IN ('queued','running')"
                        ),
                        {"prefix": args.run_id + ":%"},
                    )
                evidence.emit("draining", remaining=remaining)
                if not remaining:
                    break
                await asyncio.sleep(1)
        after = await snapshot("after")
        effect_ids = {str(row["job_id"]) for row in after["job_effects"]}
        assert len(before["jobs"]) == len(after["jobs"])
        assert all(
            (str(job["id"]) in effect_ids) == (job["status"] == "succeeded")
            for job in after["jobs"]
        )
        passed = True
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
        await asyncio.gather(*(process.wait() for process in processes))
        await engine.dispose()
        for handle in handles:
            handle.close()
        summary = {
            "passed": passed,
            "mode": "recovery",
            "original_run": args.run_id,
            "elapsed_seconds": time.perf_counter() - start,
        }
        (evidence.path / "summary.json").write_text(json.dumps(summary) + "\n")
        evidence.emit("finished", **summary)
        evidence.events.close()
        evidence.requests.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{8}T\d{6}-chaos-[0-9a-f]{8}", args.run_id):
        parser.error("Expected an exact chaos run directory name")
    args.mode = "recovery"
    asyncio.run(main(args))
