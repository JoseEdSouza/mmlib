from dataclasses import dataclass, field
from typing import Any, cast
import pandas as pd
import time


@dataclass(frozen=True)
class BenchMetrics:
    """Consolidated metrics for offline (batch) processing."""

    execution_time_ms: float
    memory_peak_mb: float
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
    partial_metrics: list["PartialOnlineBenchMetrics"] = field(default_factory=list)
    total_cpu_time_ms: float | None = None
    custom_metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "resource_scope": "client",
            "cpu_metric": "cpu_time_milliseconds",
            "memory_metric": "rss_mb",
        }
    )

    def __post_init__(self):
        if self.total_cpu_time_ms is None:
            object.__setattr__(
                self,
                "total_cpu_time_ms",
                sum(p.cpu_time_ms for p in self.partial_metrics),
            )

    @property
    def avg_points_per_step(self) -> float:
        """Average number of points processed per step."""
        return (
            self.total_points_processed / len(self.partial_metrics)
            if self.partial_metrics
            else 0.0
        )

    @classmethod
    def from_partials(
        cls,
        partials: list[PartialOnlineBenchMetrics],
        total_execution_time_ms: float,
        total_cpu_time_ms: float | None = None,
        custom_metadata: dict[str, Any] | None = None,
    ) -> "OnlineBenchMetrics":
        """Create summary metrics from a list of partial metrics."""
        total_points = sum(p.input_points_count for p in partials)
        total_yielded = len(partials)
        avg_latency = (
            sum(p.step_latency_ms for p in partials) / total_yielded
            if total_yielded > 0
            else 0
        )
        max_memory = max((p.memory_mb for p in partials), default=0)
        total_cpu_time_ms = (
            total_cpu_time_ms
            if total_cpu_time_ms is not None
            else sum(p.cpu_time_ms for p in partials)
        )

        return cls(
            total_execution_time_ms=total_execution_time_ms,
            total_cpu_time_ms=total_cpu_time_ms,
            avg_step_latency_ms=avg_latency,
            max_memory_peak_mb=max_memory,
            total_points_processed=total_points,
            total_results_yielded=total_yielded,
            partial_metrics=partials,
            custom_metadata=custom_metadata
            or {
                "resource_scope": "client",
                "cpu_metric": "cpu_time_milliseconds",
                "memory_metric": "rss_mb",
            },
        )

    def to_df(self, expand_summary: bool = False) -> pd.DataFrame:
        """
        Export partial metrics to a Pandas DataFrame.

        Args:
            expand_summary: If True, attach global summary metrics to df.attrs
                          (total_execution_time_ms, avg_step_latency_ms, etc.).
                          If False, only per-step metrics are included in the DataFrame.

        Returns:
            DataFrame with per-step metrics and derived columns (step_index,
            inter_arrival_ms, cum_points, throughput_in_pps, etc.).
        """
        # Build base table
        rows = [m.to_dict() for m in self.partial_metrics]
        df = pd.DataFrame(rows)

        matcher_name = self.custom_metadata.get("matcher_name", "unknown")
        mode = self.custom_metadata.get("mode", "online")

        if df.empty:
            # Still return an empty DF with useful metadata
            df.attrs["matcher_name"] = matcher_name
            df.attrs["mode"] = mode
            if expand_summary:
                for k, v in self._summary_attrs().items():
                    df.attrs[k] = v
            return df

        # --- Ensure identifier columns ---
        df["matcher_name"] = matcher_name
        df["mode"] = mode

        # --- Derived columns (per-row) ---
        df = cast(pd.DataFrame, df.reset_index(drop=True))
        df["step_index"] = df.index.astype(int)

        # Inter-arrival (uses wall-clock timestamp; good for burstiness diagnostics)
        # If timestamps are missing or non-numeric, this will fail loudly (good).
        df["inter_arrival_ms"] = df["timestamp"].diff() * 1000.0
        df.loc[df["step_index"] == 0, "inter_arrival_ms"] = pd.NA

        # Points per step (already provided as input_points_count, but keep a stable alias)
        if "input_points_count" in df.columns:
            df["points_per_step"] = df["input_points_count"]
        else:
            # fallback for older schemas
            df["points_per_step"] = pd.NA

        # Cumulative points processed so far (based on what the wrapper attributed to each yield)
        # This is very useful when outputs are batched.
        df["cum_points"] = (
            cast(pd.Series, pd.to_numeric(df["points_per_step"], errors="coerce"))
            .fillna(0)
            .cumsum()
        )

        # Cumulative "wait time for outputs" (sum of step latencies). Note: not identical to total_execution_time_ms
        if "step_latency_ms" in df.columns:
            df["cum_step_latency_ms"] = (
                cast(pd.Series, pd.to_numeric(df["step_latency_ms"], errors="coerce"))
                .fillna(0)
                .cumsum()
            )
        else:
            df["cum_step_latency_ms"] = pd.NA

        # --- Derived columns (global, repeated) ---
        exec_s = (
            self.total_execution_time_ms / 1000.0
            if self.total_execution_time_ms > 0
            else 0.0
        )
        throughput_in_pps = (
            (self.total_points_processed / exec_s) if exec_s > 0 else pd.NA
        )

        df["total_execution_time_ms"] = self.total_execution_time_ms
        df["total_cpu_time_ms"] = self.total_cpu_time_ms
        df["throughput_in_pps"] = throughput_in_pps
        df["avg_points_per_step"] = self.avg_points_per_step

        # Put key columns first
        front = [
            "matcher_name",
            "mode",
            "step_index",
            "timestamp",
            "step_latency_ms",
            "inter_arrival_ms",
            "points_per_step",
            "cum_points",
            "cum_step_latency_ms",
        ]
        existing_front = [c for c in front if c in df.columns]
        rest = [c for c in df.columns if c not in existing_front]
        df = cast(pd.DataFrame, df.loc[:, existing_front + rest])

        # Attach summary attrs if requested
        if expand_summary:
            for k, v in self._summary_attrs().items():
                df.attrs[k] = v

        return df

    def _summary_attrs(self) -> dict[str, Any]:
        """Helper: consistent summary attrs for DataFrame export."""
        return {
            "total_execution_time_ms": self.total_execution_time_ms,
            "avg_step_latency_ms": self.avg_step_latency_ms,
            "max_memory_peak_mb": self.max_memory_peak_mb,
            "total_points_processed": self.total_points_processed,
            "total_results_yielded": self.total_results_yielded,
            "avg_points_per_step": self.avg_points_per_step,
            "resource_scope": self.custom_metadata.get("resource_scope", "client"),
            "cpu_metric": self.custom_metadata.get(
                "cpu_metric", "cpu_time_milliseconds"
            ),
            "memory_metric": self.custom_metadata.get("memory_metric", "rss_mb"),
            "matcher_name": self.custom_metadata.get("matcher_name", "unknown"),
            "mode": self.custom_metadata.get("mode", "online"),
        }
