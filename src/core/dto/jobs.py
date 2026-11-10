import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TrainingSpec(BaseModel):
    """A bounded, deterministic simulation; dataset is a label and is never fetched."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model: str = Field(default="tiny-transformer", min_length=1, max_length=100)
    dataset: str = Field(default="synthetic-v1", min_length=1, max_length=300)
    epochs: int = Field(default=3, ge=1, le=100)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    duration_ms: int = Field(default=50, ge=0, le=30_000)
    simulate_failure: bool = False

    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def result(self) -> dict[str, Any]:
        return {"artifact_sha256": self.fingerprint(), "epochs": self.epochs, "seed": self.seed}


@dataclass(frozen=True, slots=True)
class Claim:
    job_id: UUID
    owner: str
    fence: int
    spec: TrainingSpec


@dataclass(frozen=True, slots=True)
class JobRecord:
    """A job snapshot crossing the persistence boundary without an ORM session."""

    id: UUID
    idempotency_key: str
    payload_hash: str
    payload: dict[str, Any]
    status: str
    fence: int
    lease_owner: str | None
    lease_until: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
