import asyncio
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, override

from mmlib.exceptions import MatcherConfigurationError, MatcherRuntimeError
from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result import OnlineMatchResult
from mmlib.result.offline import MatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory


class BatchesOnlineMatcher(BaseOnlineMatcher):
    """Simple batch-based online matcher without lookahead or windowing."""

    @property
    @override
    def matcher_name(self) -> str:
        return "Batches-Online"

    def __init__(
        self,
        offline_matcher: BaseMatcher,
        *,
        batch_size: int = 100,
    ):
        super().__init__()
        self.offline_matcher = offline_matcher

        if batch_size <= 0:
            raise MatcherConfigurationError("batch_size must be positive")

        self._batch_size = batch_size

        # Buffers to hold current batch and committed results
        self._current_batch: list[GPSPoint] = []
        self._committed_path: list[str] = []
        self._committed_geometry: list[Coordinate] = []
        self._all_points: list[GPSPoint] = []

    @override
    async def start(self) -> None:
        """Initialize buffers and executor."""
        if self._started:
            return

        # Create a thread pool executor for processing batches
        self._executor: Executor = ThreadPoolExecutor(max_workers=1)
        self._current_batch.clear()
        self._committed_path.clear()
        self._committed_geometry.clear()
        self._all_points.clear()
        self._started = True

    @override
    async def stop(self) -> None:
        """Clean up resources."""
        if not self._started:
            return

        # Shutdown the executor and clear buffers
        self._executor.shutdown(wait=True)
        self._current_batch.clear()
        self._committed_path.clear()
        self._committed_geometry.clear()
        self._all_points.clear()
        self._started = False

    @staticmethod
    def _dedup_list[T](items: list[T]) -> list[T]:
        """Remove consecutive duplicates from a list."""
        if not items:
            return []

        deduped = [items[0]]
        for item in items[1:]:
            if item != deduped[-1]:
                deduped.append(item)
        return deduped

    async def _process_batch(
        self, batch: list[GPSPoint], loop: asyncio.AbstractEventLoop
    ) -> MatchResult:
        """Process a single batch using the offline matcher."""
        result = await loop.run_in_executor(
            self._executor, self.offline_matcher.match, batch
        )
        return result

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """Process GPS points in fixed-size batches."""
        if not self._started:
            raise MatcherRuntimeError("Matcher must be started first")

        loop = asyncio.get_event_loop()

        async for point in points:
            self._current_batch.append(point)
            self._all_points.append(point)

            # Process the batch when it is full
            if len(self._current_batch) >= self._batch_size:
                result = await self._process_batch(self._current_batch, loop)

                if result.edge_ids:
                    self._committed_path.extend(result.edge_ids)

                if result.matched_points:
                    self._committed_geometry.extend(result.matched_points)

                # Emit incremental result
                edge_ids = self._dedup_list(self._committed_path)
                matched_points = self._dedup_list(self._committed_geometry)

                yield OnlineMatchResult(
                    self.matcher_name,
                    edge_ids=edge_ids,
                    matched_points=matched_points,
                    measurement_points=self._all_points.copy(),
                )

                # Clear the current batch
                self._current_batch.clear()

        # Process remaining points (final flush)
        if self._current_batch:
            result = await self._process_batch(self._current_batch, loop)

            if result.edge_ids:
                self._committed_path.extend(result.edge_ids)

            if result.matched_points:
                self._committed_geometry.extend(result.matched_points)

        # Emit final result with deduplication
        if self._committed_path:
            edge_ids = self._dedup_list(self._committed_path)
            matched_points = self._dedup_list(self._committed_geometry)

            yield OnlineMatchResult(
                self.matcher_name,
                edge_ids=edge_ids,
                matched_points=matched_points,
                measurement_points=self._all_points.copy(),
            )


@factory(BatchesOnlineMatcher)
def batches_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    """Factory function to create an instance of BatchesOnlineMatcher."""
    return BatchesOnlineMatcher(*args, **kwargs)
