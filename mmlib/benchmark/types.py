from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4
import pandas as pd
import time


@dataclass(frozen=True)
class BenchMetrics:
    """Consolidated metrics for offline (batch) processing."""

    execution_time_ms: float
    memory_peak_mb: float
    cpu_time_ms: float
    run_id: str = field(default_factory=lambda: uuid4().hex)
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
            "run_id": self.run_id,
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
    run_id: str = field(default_factory=lambda: uuid4().hex)
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
        run_id: str | None = None,
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
            run_id=run_id or uuid4().hex,
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

    def to_summary_row(self) -> dict[str, Any]:
        """
        Return a single-row, run-level summary (ideal for tables/aggregation).

        Conventions:
          - Identifiers (run_id, matcher_name, mode, trajectory_id, dataset_id, etc.)
            are copied from custom_metadata if present.
          - Throughput is computed from total_points_processed / total_execution_time_ms.
          - Resource metrics are client-side unless resource_scope says otherwise.
        """
        meta = self.custom_metadata

        exec_s = (
            self.total_execution_time_ms / 1000.0
            if self.total_execution_time_ms > 0
            else 0.0
        )
        throughput_in_pps = (
            (self.total_points_processed / exec_s) if exec_s > 0 else 0.0
        )

        # Keep only *useful* identifiers (avoid dumping all meta_* keys by default).
        # You can expand this allowlist as your experiment schema grows.
        id_keys = (
            "experiment_id",
            "trajectory_id",
            "dataset_id",
            "k_factor",
            "lag_points",
            "sampling_hz",
            "seed",
            "matcher_name",
            "mode",
        )
        ids: dict[str, Any] = {k: meta[k] for k in id_keys if k in meta}

        return {
            **ids,
            "run_id": self.run_id,
            "total_execution_time_ms": self.total_execution_time_ms,
            "total_cpu_time_ms": self.total_cpu_time_ms,
            "max_memory_peak_mb": self.max_memory_peak_mb,
            "total_points_processed": self.total_points_processed,
            "total_results_yielded": self.total_results_yielded,
            "avg_step_latency_ms": self.avg_step_latency_ms,
            "avg_points_per_step": self.avg_points_per_step,
            "throughput_in_pps": throughput_in_pps,
            # Keep metric semantics so you can merge server-side later
            "resource_scope": meta.get("resource_scope", "client"),
            "cpu_metric": meta.get("cpu_metric", "cpu_time_milliseconds"),
            "memory_metric": meta.get("memory_metric", "rss_mb"),
        }

    def to_df(self, expand_summary: bool = False) -> pd.DataFrame:
        """
        Export per-step metrics to a Pandas DataFrame.

        Keeps enough identifiers as columns for easy concat/plot.
        Stores run-level summary in df.attrs (optional), not repeated per row.
        """
        rows = [m.to_dict() for m in self.partial_metrics]
        df = pd.DataFrame(rows)

        meta = self.custom_metadata
        matcher_name = meta.get("matcher_name", "unknown")
        mode = meta.get("mode", "online")

        if df.empty:
            df.attrs["run_id"] = self.run_id
            df.attrs["matcher_name"] = matcher_name
            df.attrs["mode"] = mode
            if expand_summary:
                for k, v in self.to_summary_row().items():
                    df.attrs[k] = v
            return df

        df = cast(pd.DataFrame, df.reset_index(drop=True))
        df["run_id"] = self.run_id
        df["matcher_name"] = matcher_name
        df["mode"] = mode
        df["step_index"] = df.index.astype(int)

        # Derived per-step diagnostics (useful for batching/burstiness)
        df["inter_arrival_ms"] = df["timestamp"].diff() * 1000.0
        df.loc[df["step_index"] == 0, "inter_arrival_ms"] = pd.NA

        df["points_per_step"] = df["input_points_count"]
        df["cum_points"] = (
            cast(pd.Series, pd.to_numeric(df["points_per_step"], errors="coerce"))
            .fillna(0)
            .cumsum()
        )

        # Drop redundant columns / noisy meta_* columns to avoid wide DF
        drop_cols = ["input_points_count"]
        for c in drop_cols:
            if c in df.columns:
                df.drop(columns=[c], inplace=True)

        meta_cols = [c for c in df.columns if c.startswith("meta_")]
        if meta_cols:
            df.drop(columns=meta_cols, inplace=True)

        # Column order
        front = [
            "run_id",
            "matcher_name",
            "mode",
            "step_index",
            "timestamp",
            "step_latency_ms",
            "inter_arrival_ms",
            "points_per_step",
            "cum_points",
            "memory_mb",
            "cpu_time_ms",  # optional but kept here as diagnostic
        ]
        existing_front = [c for c in front if c in df.columns]
        rest = [c for c in df.columns if c not in existing_front]
        df = cast(pd.DataFrame, df.loc[:, existing_front + rest])

        if expand_summary:
            for k, v in self.to_summary_row().items():
                df.attrs[k] = v

        return df
