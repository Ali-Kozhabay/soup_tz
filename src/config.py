from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOUP_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://soup:soup@localhost:5432/soup"
    redis_url: str = "redis://localhost:6379/0"
    redis_channel: str = "soup:jobs:ready"
    lease_seconds: float = Field(default=6, ge=0.5, le=300)
    heartbeat_seconds: float = Field(default=1, ge=0.05)
    poll_seconds: float = Field(default=0.5, ge=0.01, le=10)
    max_attempts: int = Field(default=25, ge=1, le=1000)
    pool_size: int = Field(default=5, ge=1, le=50)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_lease(self) -> "Settings":
        if self.heartbeat_seconds * 2 >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be less than half of lease_seconds")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use postgresql+asyncpg")
        return self
