from dataclasses import dataclass, field
from typing import Any, cast
import pandas as pd
import time


@dataclass(frozen=True)
class BenchMetrics:
    """Consolidated metrics for offline (batch) processing."""

    execution_time_ms: float
    memory_peak_mb: float
    memory_delta_mb: float
    cpu_time_ms: float
    timestamp: float = field(default_factory=time.time)
    custom_metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "resource_scope": "client",
            "cpu_metric": "cpu_time_milliseconds",
            "memory_metric": "rss_mb",
        }
    )

    def to_df(self) -> pd.DataFrame:
        """Export metrics to a Pandas DataFrame."""
        data = {
            "matcher_name": self.custom_metadata.get("matcher_name", "unknown"),
            "mode": self.custom_metadata.get("mode", "offline"),
            "execution_time_ms": [self.execution_time_ms],
            "cpu_time_ms": [self.cpu_time_ms],
            "memory_peak_mb": [self.memory_peak_mb],
            "memory_delta_mb": [self.memory_delta_mb],
            "timestamp": [self.timestamp],
        }
        for k, v in self.custom_metadata.items():
            if k not in ["matcher_name", "mode"]:
                data[f"meta_{k}"] = [v]
        return pd.DataFrame(data)


@dataclass(frozen=True)
class PartialOnlineBenchMetrics:
    """Incremental metrics for each 'yield' during the stream."""

    step_latency_ms: float
    memory_mb: float
    memory_delta_mb: float
    cpu_time_ms: float
    timestamp: float = field(default_factory=time.time)
    input_points_indices: list[int] = field(default_factory=list)
    input_points_count: int = 0
    custom_metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "resource_scope": "client",
            "cpu_metric": "cpu_time_milliseconds",
            "memory_metric": "rss_mb",
        }
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to a flat dictionary for DataFrame inclusion."""
        d = {
            "step_latency_ms": self.step_latency_ms,
            "cpu_time_ms": self.cpu_time_ms,
            "memory_mb": self.memory_mb,
            "memory_delta_mb": self.memory_delta_mb,
            "timestamp": self.timestamp,
            "input_points_count": self.input_points_count,
        }
        for k, v in self.custom_metadata.items():
            d[f"meta_{k}"] = v
        return d


@dataclass(frozen=True)
class OnlineBenchMetrics:
    """Consolidated summary of a complete streaming session."""

    total_execution_time_ms: float
    avg_step_latency_ms: float
    max_memory_peak_mb: float
    total_points_processed: int
    total_results_yielded: int
    partial_metrics: list[PartialOnlineBenchMetrics] = field(default_factory=list)
    custom_metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "resource_scope": "client",
            "cpu_metric": "cpu_time_milliseconds",
            "memory_metric": "rss_mb",
        }
    )

    def to_df(self, expand_summary: bool = False) -> pd.DataFrame:
        """Export all partial metrics to a Pandas DataFrame, including summary stats in metadata."""
        rows = [m.to_dict() for m in self.partial_metrics]
        df = pd.DataFrame(rows)

        # Ensure common columns exist
        if not df.empty:
            df["matcher_name"] = self.custom_metadata.get("matcher_name", "unknown")
            df["mode"] = self.custom_metadata.get("mode", "online")
            # Move these columns to the front
            cols = ["matcher_name", "mode"] + [
                c for c in df.columns if c not in ["matcher_name", "mode"]
            ]
            df = cast(pd.DataFrame, df.loc[:, cols])

        if expand_summary:
            df.attrs["total_execution_time_ms"] = self.total_execution_time_ms
            df.attrs["avg_step_latency_ms"] = self.avg_step_latency_ms
            df.attrs["max_memory_peak_mb"] = self.max_memory_peak_mb
            df.attrs["total_points_processed"] = self.total_points_processed
            df.attrs["total_results_yielded"] = self.total_results_yielded
            df.attrs["avg_points_per_step"] = (
                self.total_points_processed / len(self.partial_metrics)
                if self.partial_metrics
                else 0
            )
            df.attrs["avg_delta_memory_mb"] = (
                sum(m.memory_delta_mb for m in self.partial_metrics)
                / len(self.partial_metrics)
                if self.partial_metrics
                else 0
            )

        return df
