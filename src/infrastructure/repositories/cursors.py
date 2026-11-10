import base64
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ValidationError

from src.core.exceptions import InvalidCursor


class Cursor(BaseModel):
    timestamp: datetime
    job_id: UUID
    ceiling: datetime
    status: str | None = None

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, value: str, status: str | None) -> "Cursor":
        try:
            if len(value) > 1024:
                raise ValueError("Cursor too long")
            cursor = cls.model_validate_json(base64.b64decode(value, altchars=b"-_", validate=True))
            if cursor.timestamp.tzinfo is None or cursor.ceiling.tzinfo is None:
                raise ValueError("Timezone required")
            if cursor.status != status or cursor.timestamp > cursor.ceiling:
                raise ValueError("Cursor does not match query")
            return cursor
        except (ValueError, ValidationError) as exc:
            raise InvalidCursor("Invalid cursor or changed status filter") from exc
