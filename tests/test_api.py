import asyncio
from uuid import uuid4

from src.core.dto.jobs import TrainingSpec
from src.main import create_app


async def test_create_replay_conflict_and_validation(client):
    response = await client.post("/v1/jobs", json={}, headers={"Idempotency-Key": "api-key"})
    assert response.status_code == 201
    job_id = response.json()["id"]
    assert response.headers["location"] == f"/v1/jobs/{job_id}"
    assert response.headers["x-request-id"]
    replay = await client.post(
        "/v1/jobs", json=TrainingSpec().model_dump(), headers={"Idempotency-Key": "api-key"}
    )
    assert replay.status_code == 200 and replay.json()["id"] == job_id
    assert replay.headers["idempotency-replayed"] == "true"
    conflict = await client.post(
        "/v1/jobs", json={"epochs": 4}, headers={"Idempotency-Key": "api-key"}
    )
    assert conflict.status_code == 409
    assert (await client.post("/v1/jobs", json={})).status_code == 422
    for payload in ({"duration_ms": -1}, {"duration_ms": True}, {"unknown": 1}):
        assert (
            await client.post("/v1/jobs", json=payload, headers={"Idempotency-Key": "invalid"})
        ).status_code == 422
    assert (await client.get(f"/v1/jobs/{uuid4()}")).status_code == 404
    assert (await client.get("/v1/jobs/not-a-uuid")).status_code == 422


async def test_concurrent_http_retries(client):
    responses = await asyncio.gather(
        *(client.post("/v1/jobs", json={}, headers={"Idempotency-Key": "burst"}) for _ in range(24))
    )
    assert sum(response.status_code == 201 for response in responses) == 1
    assert all(response.status_code in {200, 201} for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1


async def test_keyset_pagination_has_no_gaps_or_duplicates(client):
    ids = set()
    for i in range(7):
        response = await client.post("/v1/jobs", json={}, headers={"Idempotency-Key": f"page-{i}"})
        ids.add(response.json()["id"])
    first = (await client.get("/v1/jobs?limit=3")).json()
    seen = [job["id"] for job in first["items"]]
    # A later insert is outside this traversal's high-water mark.
    await client.post("/v1/jobs", json={}, headers={"Idempotency-Key": "later"})
    cursor = first["next_cursor"]
    while cursor:
        page = (await client.get("/v1/jobs", params={"limit": 3, "cursor": cursor})).json()
        seen.extend(job["id"] for job in page["items"])
        cursor = page["next_cursor"]
    assert set(seen) == ids and len(seen) == 7
    assert (await client.get("/v1/jobs?limit=0")).status_code == 422
    assert (await client.get("/v1/jobs?cursor=garbage")).status_code == 400
    assert (
        await client.get("/v1/jobs", params={"cursor": first["next_cursor"], "status": "running"})
    ).status_code == 400


async def test_api_cancel_status_and_terminal_conflict(client, repository):
    response = await client.post("/v1/jobs", json={}, headers={"Idempotency-Key": "cancel-api"})
    job_id = response.json()["id"]
    assert (await client.post(f"/v1/jobs/{job_id}/cancel")).json()["status"] == "cancelled"
    assert (await client.get(f"/v1/jobs/{job_id}")).json()["status"] == "cancelled"
    assert (await client.post(f"/v1/jobs/{job_id}/cancel")).status_code == 200
    job, _ = await repository.create("completed", TrainingSpec())
    claim = await repository.claim("worker")
    await repository.complete(claim)
    assert (await client.post(f"/v1/jobs/{job.id}/cancel")).status_code == 409
    assert (await client.post(f"/v1/jobs/{uuid4()}/cancel")).status_code == 404


async def test_redis_outage_does_not_lose_committed_jobs(repository, settings):
    import httpx

    app = create_app(settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"}))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        result = await api.post("/v1/jobs", json={}, headers={"Idempotency-Key": "redis-down"})
        assert result.status_code == 201
        ready = await api.get("/health/ready")
        assert ready.status_code == 200 and ready.json()["status"] == "degraded"
    claim = await repository.claim("poller")
    assert str(claim.job_id) == result.json()["id"]
    assert await repository.complete(claim)
