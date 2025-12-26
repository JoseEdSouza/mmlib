import asyncio

class _Synchronizer:
    """Synchronizes sending and receiving of points for 1:1 matching."""

    def __init__(self, max_inflight: int = 1) -> None:
        self._max_inflight = max_inflight
        self._inflight_count = 0
        self._sent_count = 0
        self._received_count = 0
        self._condition = asyncio.Condition()

    async def wait_to_send(self) -> None:
        """Blocks until the number of in-flight messages is below the limit."""
        async with self._condition:
            await self._condition.wait_for(lambda: self._inflight_count < self._max_inflight)

    async def notify_sent(self) -> None:
        """Notifies the synchronizer that a message has been sent."""
        async with self._condition:
            self._inflight_count += 1
            self._sent_count += 1
            self._condition.notify_all()

    async def notify_received(self) -> None:
        """Notifies the synchronizer that a message has been received."""
        async with self._condition:
            if self._inflight_count > 0:
                self._inflight_count -= 1
            self._received_count += 1
            self._condition.notify_all()

    async def is_finished(self, sending_done: bool) -> bool:
        """Checks if all sent messages have been received."""
        async with self._condition:
            return sending_done and self._received_count >= self._sent_count

    @property
    def sent_count(self) -> int:
        return self._sent_count

    @property
    def received_count(self) -> int:
        return self._received_count
