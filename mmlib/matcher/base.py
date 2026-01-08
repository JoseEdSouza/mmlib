from abc import ABC, abstractmethod
from typing import AsyncIterable, AsyncIterator, Self
from uuid import uuid4

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
    def run_id(self) -> str:
        """An optional run identifier for benchmarking purposes."""
        if getattr(self, "_run_id", None) is None:
            self._run_id = uuid4().hex
        return self._run_id

    @run_id.setter
    def run_id(self, value: str) -> None:
        self._run_id = value

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

    def bench_match(
        self,
        points: list[GPSPoint],
    ) -> tuple[MatchResult, BenchMetrics]:
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
    def run_id(self) -> str:
        """An optional run identifier for benchmarking purposes."""
        if getattr(self, "_run_id", None) is None:
            self._run_id = uuid4().hex
        return self._run_id

    @run_id.setter
    def run_id(self, value: str) -> None:
        self._run_id = value

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
        Run stream matching, emitting partial metrics per step.
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

                # Populate indices consumed since the last yield
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
        first_output_perf: float | None = None

        async with self:
            async for res, partial in self.bench_match_stream(_gen()):
                if first_output_perf is None:
                    first_output_perf = time.perf_counter()

                last_result = res
                partial_results.append(partial)
                peak_mem = max(peak_mem, partial.memory_mb)

        t1 = time.perf_counter()
        end_metrics = collect_process_metrics()

        if last_result is None:
            raise MatcherRuntimeError("bench_match_stream emitted no results")

        total_execution_time_ms = (t1 - t0) * 1000
        total_cpu_time_ms = end_metrics["cpu_time_ms"] - start_metrics["cpu_time_ms"]

        ttff_ms = None
        if first_output_perf is not None:
            ttff_ms = (first_output_perf - t0) * 1000

        summary = OnlineBenchMetrics.from_partials(
            run_id=self.run_id,
            partials=partial_results,
            total_execution_time_ms=total_execution_time_ms,
            total_cpu_time_ms=total_cpu_time_ms,
            custom_metadata={
                "run_id": self.run_id,
                "matcher_name": self.matcher_name,
                "mode": "online",
                "n_input_points": len(points),
                "memory_peak_sampled_mb": peak_mem,
                "ttff_ms": ttff_ms,
                "n_emissions": len(partial_results),
            },
        )

        return last_result, summary
