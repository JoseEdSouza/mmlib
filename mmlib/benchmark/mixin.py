import time
from contextlib import contextmanager
from typing import Generator
from mmlib.benchmark.types import (
    BenchMetrics,
    PartialOnlineBenchMetrics,
)
from mmlib.benchmark.utils import collect_process_metrics


class BenchmarkMixin:
    """Mixin to add benchmarking capabilities to matchers."""

    @contextmanager
    def _measure_offline(self) -> Generator[None, None, None]:
        """
        Context manager to measure offline execution metrics.
        The resulting metrics are stored in self._last_bench_metrics.
        """
        # Start measurements
        start_metrics = collect_process_metrics()
        t0 = time.perf_counter()

        try:
            yield
        finally:
            t1 = time.perf_counter()
            # Capture final states
            end_metrics = collect_process_metrics()

            # For a pragmatic peak, we use the end memory if it grew,
            # or start if it didn't. In a more complex version we'd monitor.
            peak_mem = max(start_metrics["memory_mb"], end_metrics["memory_mb"])
            delta_mem = end_metrics["memory_mb"] - start_metrics["memory_mb"]

            metrics = BenchMetrics(
                execution_time_ms=(t1 - t0) * 1000,
                memory_peak_mb=peak_mem,
                memory_delta_mb=delta_mem,
                cpu_time_ms=end_metrics["cpu_time_ms"] - start_metrics["cpu_time_ms"],
                timestamp=end_metrics["timestamp"],
                custom_metadata={
                    "matcher_name": getattr(self, "matcher_name", "unknown"),
                    "mode": "offline",
                },
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
        start_metrics = collect_process_metrics()
        t0 = time.perf_counter()

        input_indices: list[int] = []

        try:
            yield input_indices
        finally:
            t1 = time.perf_counter()
            end_metrics = collect_process_metrics()
            delta_mem = end_metrics["memory_mb"] - start_metrics["memory_mb"]

            self._last_partial = PartialOnlineBenchMetrics(
                step_latency_ms=(t1 - t0) * 1000,
                memory_mb=end_metrics["memory_mb"],
                memory_delta_mb=delta_mem,
                cpu_time_ms=end_metrics["cpu_time_ms"] - start_metrics["cpu_time_ms"],
                timestamp=end_metrics["timestamp"],
                input_points_indices=input_indices,
                input_points_count=len(input_indices),
                custom_metadata={
                    "matcher_name": getattr(self, "matcher_name", "unknown"),
                    "mode": "online",
                },
            )

    def _get_last_partial(self) -> PartialOnlineBenchMetrics:
        if not hasattr(self, "_last_partial"):
            raise ValueError("No partial metrics recorded.")
        return self._last_partial
