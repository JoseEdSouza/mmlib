from abc import ABC, abstractmethod
from typing import AsyncIterable, AsyncIterator, Self

from mmlib.exceptions import MatcherInputError, MatcherRuntimeError
from mmlib.result import MatchResult, OnlineMatchResult
from mmlib.types import GPSPoint
from mmlib.benchmark import (
    BenchmarkMixin,
    BenchMetrics,
    PartialOnlineBenchMetrics,
    OnlineBenchMetrics,
)


class BaseMatcher(ABC, BenchmarkMixin):
    """BaseMatcher is a base class for map matching implementations."""

    @property
    @abstractmethod
    def matcher_name(self) -> str:
        """The name of the matcher."""
        ...

    @abstractmethod
    def match(self, points: list[GPSPoint]) -> MatchResult:
        """Map match the provided GPX points.

        Args:
            points (list[GPSPoint]): The GPX points to the map match.

        Returns:
            MatchResult: The result of the map matching process.
        """
        ...

    def bench_match(self, points: list[GPSPoint]) -> tuple[MatchResult, BenchMetrics]:
        """
        Execute map matching while collecting global performance metrics.

        Returns:
            tuple: (The matching result, the collected metrics)
        """
        with self._measure_offline():
            result = self.match(points)
        return result, self._get_last_metrics()


class BaseOnlineMatcher(ABC, BenchmarkMixin):
    """BaseClass for online map matching implementations."""

    def __init__(self) -> None:
        super().__init__()
        self._started: bool = False

    @property
    @abstractmethod
    def matcher_name(self) -> str:
        """The name of the matcher."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Initialize resources."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Clean up resources."""
        ...

    async def __aenter__(self) -> Self:
        """Initialize resources"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool | None:
        """Clean up resources."""
        await self.stop()
        return None

    @abstractmethod
    def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """
        Match a stream of GPS points.
        Args:
            points (AsyncIterable[GPSPoint]): An async iterable of GPS points.

        Returns:
            AsyncIterator[OnlineMatchResult]: An async iterator of OnlineMatchResult.
        """
        ...

    async def bench_match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[tuple[OnlineMatchResult, PartialOnlineBenchMetrics]]:
        """
        Executa o matching em stream, emitindo métricas parciais por etapa.
        Args:
            points (AsyncIterable[GPSPoint]): An async iterable of GPS points.

        Yields:
            tuple: (OnlineMatchResult, PartialOnlineBenchMetrics) for each matching step.
        """
        point_counter = 0
        last_yield_point_count = 0

        async def _point_wrapper():
            nonlocal point_counter
            async for p in points:
                point_counter += 1
                yield p

        matcher_stream = self.match_stream(_point_wrapper())

        while True:
            with self._measure_online_step() as indices:
                try:
                    result = await anext(matcher_stream)
                except StopAsyncIteration:
                    break

                # Popula os índices consumidos desde o último yield
                indices.extend(range(last_yield_point_count, point_counter))
                last_yield_point_count = point_counter

            yield result, self._get_last_partial()

    async def match_batch(self, points: list[GPSPoint]) -> OnlineMatchResult:
        """
        Match a batch of GPS points.
        Args:
            points (list[GPSPoint]): A list of GPS points.
        Returns:
            OnlineMatchResult: The result of the online map matching process.
        """
        if not points:
            raise MatcherInputError("points must not be empty")

        async def _gen() -> AsyncIterator[GPSPoint]:
            for p in points:
                yield p

        last: OnlineMatchResult | None = None
        async with self:
            async for result in self.match_stream(_gen()):
                last = result

        # If points is not empty, "last is None" indicates a bug/broken contract
        if last is None:
            raise MatcherRuntimeError(
                "match_stream emitted no results for non-empty input"
            )

        return last

    async def bench_match_batch(
        self, points: list[GPSPoint]
    ) -> tuple[OnlineMatchResult, OnlineBenchMetrics]:
        """
        Match a batch of GPS points while collecting comprehensive online performance metrics.

        Args:
            points (list[GPSPoint]): A list of GPS points to match.

        Returns:
            tuple: (OnlineMatchResult, OnlineBenchMetrics) with consolidated metrics.
        """
        if not points:
            raise MatcherInputError("points must not be empty")

        async def _gen() -> AsyncIterator[GPSPoint]:
            for p in points:
                yield p

        partial_results: list[PartialOnlineBenchMetrics] = []
        last_result: OnlineMatchResult | None = None

        from mmlib.benchmark.utils import collect_process_metrics
        import time

        start_metrics = collect_process_metrics()
        t0 = time.perf_counter()
        peak_mem = start_metrics["memory_mb"]

        async with self:
            async for res, partial in self.bench_match_stream(_gen()):
                last_result = res
                partial_results.append(partial)
                peak_mem = max(peak_mem, partial.memory_mb)

        t1 = time.perf_counter()
        avg_latency_ms = (
            sum(p.step_latency_ms for p in partial_results) / len(partial_results)
            if partial_results
            else 0
        )

        if last_result is None:
            raise MatcherRuntimeError("bench_match_stream emitted no results")

        # Create the consolidated metrics from mmlib.benchmark
        from mmlib.benchmark import OnlineBenchMetrics

        summary = OnlineBenchMetrics(
            total_execution_time_ms=(t1 - t0) * 1000,
            avg_step_latency_ms=avg_latency_ms,
            max_memory_peak_mb=peak_mem,
            total_points_processed=len(points),
            total_results_yielded=len(partial_results),
            partial_metrics=partial_results,
            custom_metadata={
                "matcher_name": self.matcher_name,
                "mode": "online",
            },
        )

        return last_result, summary
