from typing import Protocol


class Wakeup(Protocol):
    async def publish(self) -> None: ...
