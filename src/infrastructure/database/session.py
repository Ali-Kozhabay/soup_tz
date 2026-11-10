from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        isolation_level="READ COMMITTED",
        pool_size=settings.pool_size,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args={
            "timeout": 5,
            "command_timeout": 10,
            "server_settings": {
                "application_name": "soup-queue",
                "timezone": "UTC",
                "statement_timeout": "10000",
                "lock_timeout": "3000",
                "idle_in_transaction_session_timeout": "10000",
            },
        },
    )


def make_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
