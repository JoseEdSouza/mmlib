import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterable, AsyncGenerator

from mmlib.types import Coordinate, GPSPoint
from mmlib.result.base import BaseMatchResult


@dataclass
class OnlineMatchResult(BaseMatchResult):
    # Queue contains matched points plus a sentinel (None) to signal completion
    _queue: asyncio.Queue[Coordinate | None] = field(default_factory=asyncio.Queue)
    _finished: bool = False

    async def _emit(self, value: Coordinate):
        await self._queue.put(value)

    def finish(self):
        """Mark the stream as finished."""
        self._finished = True
        self._queue.put_nowait(None)  # sentinel

    def _stream(self) -> AsyncGenerator[Coordinate, None]:
        """Internal async generator that yields matched points in real time."""

        async def gen():
            while True:
                item = await self._queue.get()
                if item is None:  # sentinel
                    break
                yield item

        return gen()

    def __aiter__(self) -> AsyncIterable[Coordinate]:
        return self._stream()

    def _update(
        self,
        new_point: GPSPoint | Coordinate | None,
        matched_point: Coordinate | None = None,
        edge_id: str | None = None,
    ):
        """Updates state AND emits matched points into the async stream."""
        if new_point:
            if isinstance(new_point, GPSPoint):
                self.measurement_points.append(new_point)
            else:
                # Manually construct GPSPoint with a default time if strict typing requires non-None
                # Or update GPSPoint definition to allow None time (it usually does or uses Optional)
                # Assuming GPSPoint.time is datetime | None:
                from datetime import datetime

                self.measurement_points.append(
                    GPSPoint(lat=new_point.lat, lon=new_point.lon, time=datetime.now())
                )

        if matched_point:
            self.matched_points.append(matched_point)
            asyncio.create_task(self._emit(matched_point))

        if edge_id:
            self.edge_ids.append(edge_id)
