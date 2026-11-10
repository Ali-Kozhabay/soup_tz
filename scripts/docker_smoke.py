"""Run inside the API container to check HTTP, workers, Redis, and persisted effects."""

import asyncio
import json
from uuid import uuid4

import httpx
from sqlalchemy import text

from src.config import Settings
from src.infrastructure.database.session import make_engine


async def main():
    prefix = f"docker-smoke-{uuid4().hex}"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=10) as client:
        ready = await client.get("/health/ready")
        assert ready.status_code == 200 and ready.json()["status"] == "ok"
        print(json.dumps({"check": "readiness", "response": ready.json()}), flush=True)

        async def create(suffix, payload):
            response = await client.post(
                "/v1/jobs", json=payload, headers={"Idempotency-Key": f"{prefix}-{suffix}"}
            )
            assert response.status_code == 201, response.text
            assert response.headers["x-request-id"]
            return response.json()

        success = await create("success", {"duration_ms": 2000})
        failure = await create("failure", {"simulate_failure": True})
        cancelled = await create("cancelled", {"duration_ms": 5000})
        replay = await client.post(
            "/v1/jobs", json={"duration_ms": 2000}, headers={"Idempotency-Key": f"{prefix}-success"}
        )
        assert replay.status_code == 200 and replay.json()["id"] == success["id"]
        conflict = await client.post(
            "/v1/jobs", json={"seed": 9}, headers={"Idempotency-Key": f"{prefix}-success"}
        )
        assert conflict.status_code == 409
        print(
            json.dumps(
                {
                    "check": "idempotency",
                    "replay": replay.status_code,
                    "conflict": conflict.status_code,
                }
            ),
            flush=True,
        )

        async with asyncio.timeout(15):
            # Poll the real worker state; no in-process event can signal this transition.
            while (await client.get(f"/v1/jobs/{cancelled['id']}")).json()["status"] != "running":  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        response = await client.post(f"/v1/jobs/{cancelled['id']}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelled"

        expected = {
            success["id"]: "succeeded",
            failure["id"]: "failed",
            cancelled["id"]: "cancelled",
        }
        async with asyncio.timeout(30):
            while True:
                observed = {
                    job_id: (await client.get(f"/v1/jobs/{job_id}")).json()["status"]
                    for job_id in expected
                }
                if observed == expected:
                    break
                await asyncio.sleep(0.1)
        print(json.dumps({"check": "terminal_states", "states": observed}), flush=True)

        seen = set()
        cursor = None
        while True:
            page = await client.get(
                "/v1/jobs", params={"limit": 1, **({"cursor": cursor} if cursor else {})}
            )
            assert page.status_code == 200
            seen.update(job["id"] for job in page.json()["items"])
            cursor = page.json()["next_cursor"]
            if not cursor:
                break
        assert set(expected) <= seen

    engine = make_engine(Settings())
    try:
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT j.id, j.status, count(e.job_id) AS effects FROM jobs j "
                            "LEFT JOIN job_effects e ON e.job_id=j.id "
                            "WHERE j.idempotency_key LIKE :prefix "
                            "GROUP BY j.id, j.status"
                        ),
                        {"prefix": prefix + "%"},
                    )
                )
                .mappings()
                .all()
            )
            assert len(rows) == 3
            assert all(row["effects"] == int(row["status"] == "succeeded") for row in rows)
            unfinished = await connection.scalar(
                text(
                    "SELECT count(*) FROM job_attempts a JOIN jobs j ON j.id=a.job_id "
                    "WHERE j.idempotency_key LIKE :prefix AND a.ended_at IS NULL"
                ),
                {"prefix": prefix + "%"},
            )
            assert unfinished == 0
            print(
                json.dumps(
                    {
                        "check": "database_effects",
                        "jobs": 3,
                        "effects": 1,
                        "unfinished_attempts": unfinished,
                    }
                ),
                flush=True,
            )
    finally:
        await engine.dispose()
    print(
        json.dumps(
            {
                "passed": True,
                "checks": [
                    "readiness",
                    "create",
                    "get",
                    "pagination",
                    "idempotent_replay",
                    "conflict",
                    "running_cancellation",
                    "success",
                    "failure",
                    "database_effect_cardinality",
                ],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
