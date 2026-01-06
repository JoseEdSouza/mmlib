import time
import psutil
from contextlib import contextmanager
from typing import Generator
from mmlib.benchmark.types import (
    BenchMetrics,
    PartialOnlineBenchMetrics,
)


class BenchmarkMixin:
    """Mixin to add benchmarking capabilities to matchers."""

    @contextmanager
    def _measure_offline(self) -> Generator[None, None, None]:
        """
        Context manager to measure offline execution metrics.
        The resulting metrics are stored in self._last_bench_metrics.
        """
        process = psutil.Process()

        # Start measurements
        start_time = time.perf_counter()
        # Initialize CPU percent measurement
        process.cpu_percent(interval=None)
        start_mem = process.memory_info().rss / (1024 * 1024)

        try:
            yield
        finally:
            end_time = time.perf_counter()
            # Capture final states
            cpu_usage = process.cpu_percent(interval=None)
            end_mem = process.memory_info().rss / (1024 * 1024)

            # For a pragmatic peak, we use the end memory if it grew,
            # or start if it didn't. In a more complex version we'd monitor.
            peak_mem = max(start_mem, end_mem)

            metrics = BenchMetrics(
                execution_time_s=end_time - start_time,
                memory_peak_mb=peak_mem,
                cpu_usage_percent=cpu_usage,
            )
            self._last_bench_metrics = metrics

    def _get_last_metrics(self) -> BenchMetrics:
        if not hasattr(self, "_last_bench_metrics"):
            raise ValueError("No metrics have been recorded yet.")
        return self._last_bench_metrics

    @contextmanager
    def _measure_online_step(self) -> Generator[list[int], None, None]:
        """
        Context manager to measure a single step in a streaming process.
        Returns a list that should be populated with input indices.
        """
        process = psutil.Process()
        start_time = time.perf_counter()
        process.cpu_percent(interval=None)

        input_indices: list[int] = []

        try:
            yield input_indices
        finally:
            end_time = time.perf_counter()
            cpu_usage = process.cpu_percent(interval=None)
            mem = process.memory_info().rss / (1024 * 1024)

            self._last_partial = PartialOnlineBenchMetrics(
                step_latency_s=end_time - start_time,
                memory_current_mb=mem,
                cpu_usage_percent=cpu_usage,
                input_points_indices=input_indices,
                input_points_count=len(input_indices),
            )

    def _get_last_partial(self) -> PartialOnlineBenchMetrics:
        if not hasattr(self, "_last_partial"):
            raise ValueError("No partial metrics recorded.")
        return self._last_partial
