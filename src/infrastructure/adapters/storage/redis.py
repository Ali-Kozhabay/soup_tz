import asyncio
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class RedisWakeup:
    """Best-effort hints. Timed database polling is always the recovery path."""

    def __init__(self, url: str, channel: str) -> None:
        self.redis: Redis = Redis.from_url(url, socket_connect_timeout=0.2)
        self.channel = channel
        self.event = asyncio.Event()

    async def publish(self) -> None:
        try:
            async with asyncio.timeout(0.25):
                await self.redis.publish(self.channel, "ready")
        except (RedisError, TimeoutError, OSError):
            logger.warning("redis_publish_unavailable")

    async def listen(self) -> None:
        while True:
            try:
                async with self.redis.pubsub() as subscription:
                    await subscription.subscribe(self.channel)
                    async for message in subscription.listen():
                        if message["type"] == "message":
                            self.event.set()
            except (RedisError, OSError):
                logger.warning("redis_subscription_unavailable")
                await asyncio.sleep(1)

    async def wait(self, poll_seconds: float) -> None:
        try:
            async with asyncio.timeout(poll_seconds):
                await self.event.wait()
        except TimeoutError:
            pass
        finally:
            self.event.clear()

    async def close(self) -> None:
        await self.redis.aclose()
