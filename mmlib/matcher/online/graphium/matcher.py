import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, Final

import requests

from mmlib.matcher.base import BaseOnlineMatcher
from mmlib.matcher.offline.graphium import GraphiumOfflineMatcher
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory


class GraphiumOnlineMatcher(BaseOnlineMatcher):
    """Online matcher that batches points and delegates to Graphium."""

    _matcher_name: Final[str] = "graphium_online"

    def __init__(
        self,
        base_url: str,
        graph_name: str,
        *,
        version: str = "current",
        timeout_s: float = 60.0,
        batch_size: int = 10,
        cold_start: int | None = 30,
        remainder_points: bool = True,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("Batch size must be a positive integer.")
        if cold_start is not None and cold_start < 0:
            raise ValueError("Cold start must be a non-negative integer or None.")

        super().__init__()

        self._offline_matcher = GraphiumOfflineMatcher(
            base_url=base_url,
            graph_name=graph_name,
            version=version,
            timeout_s=timeout_s,
        )
        self._cold_start = max(batch_size, cold_start) if cold_start else None
        self._batch_size = batch_size
        self._points_buffer: list[GPSPoint] = []
        self._enable_remainder_points = remainder_points
        self._remainder_points: list[GPSPoint] = []
        self._committed_geometry: list[Coordinate] = []
        self._committed_edge_ids: list[str] = []
        self._all_points: list[GPSPoint] = []

    @property
    def matcher_name(self) -> str:
        return self._matcher_name

    async def start(self) -> None:
        """Initialize ThreadPoolExecutor and Requests Session."""
        if not self._started:
            self._executor = ThreadPoolExecutor(max_workers=1)
            self._session = requests.Session()
            self._points_buffer.clear()
            self._all_points.clear()
            self._committed_geometry.clear()
            self._committed_edge_ids.clear()
            self._remainder_points.clear()
            self._started = True

    async def stop(self) -> None:
        """Clean up resources."""
        if self._executor:
            self._executor.shutdown(wait=True)

        if self._session:
            self._session.close()

        self._all_points.clear()
        self._points_buffer.clear()
        self._committed_geometry.clear()
        self._committed_edge_ids.clear()
        self._remainder_points.clear()

        self._started = False

    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """
        Consumes the point stream, sending requests in separate threads,
        but awaiting responses to maintain sequence (startSegmentId).
        """
        await self.start()

        current_start_segment_id: str | None = None
        remainder_points: list[GPSPoint] | None = None
        loop = asyncio.get_running_loop()
        cold_start_reached = False
        async for point in points:
            # Accumulate points in buffer
            self._points_buffer.append(point)
            self._all_points.append(point)

            if (
                (not cold_start_reached)
                and (self._cold_start is not None)
                and (len(self._all_points) < self._cold_start)
            ):
                continue  # Wait for cold start

            if not cold_start_reached:
                cold_start_reached = True

            if len(self._points_buffer) < self._batch_size:
                continue  # Wait to fill the buffer

            batch = (
                remainder_points + self._points_buffer
                if remainder_points
                else self._points_buffer
            )

            result, new_segment_id = await self._process_batch(
                batch, loop, current_start_segment_id
            )

            if new_segment_id is not None:
                current_start_segment_id = new_segment_id

            if self._enable_remainder_points:
                retain = min(5, self._batch_size)
                remainder_points = self._points_buffer[-retain:]

            self._points_buffer.clear()

            yield result

        # Process remaining points
        if self._points_buffer:
            batch = (remainder_points or []) + self._points_buffer
            print(f"Processing final batch of {len(batch)} points.")
            result, _ = await self._process_batch(batch, loop, current_start_segment_id)
            yield result

    async def _process_batch(
        self,
        batch: list[GPSPoint],
        loop: asyncio.AbstractEventLoop,
        current_start_segment_id: str | None,
    ) -> tuple[OnlineMatchResult, str | None]:
        """Process a batch of points and return the match result."""
        extra_params = (
            {"startSegmentId": current_start_segment_id}
            if current_start_segment_id
            else None
        )

        # Run matching in thread pool executor
        data, last_segment_id = await loop.run_in_executor(
            self._executor,
            self._offline_matcher.match_with_extra_params,
            batch,
            extra_params,
            self._session,
        )

        self._committed_geometry.extend(data.matched_points)
        self._committed_edge_ids.extend(data.edge_ids)

        return (
            OnlineMatchResult(
                matcher_name=self.matcher_name,
                measurement_points=self._all_points.copy(),
                matched_points=self._committed_geometry.copy(),
                edge_ids=self._committed_edge_ids.copy(),
            ),
            last_segment_id,
        )


@factory(GraphiumOnlineMatcher)
def graphium_online_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return GraphiumOnlineMatcher(*args, **kwargs)
