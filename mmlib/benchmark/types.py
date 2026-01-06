from dataclasses import dataclass, field
from typing import Any
import pandas as pd


@dataclass(frozen=True)
class BenchMetrics:
    """Métricas consolidadas para processamento offline (batch)."""

    execution_time_s: float
    memory_peak_mb: float
    cpu_usage_percent: float
    custom_metadata: dict[str, Any] = field(default_factory=dict)

    def to_df(self) -> pd.DataFrame:
        """Export metrics to a Pandas DataFrame."""
        data = {
            "execution_time_s": [self.execution_time_s],
            "memory_peak_mb": [self.memory_peak_mb],
            "cpu_usage_percent": [self.cpu_usage_percent],
        }
        for k, v in self.custom_metadata.items():
            data[f"meta_{k}"] = [v]
        return pd.DataFrame(data)


@dataclass(frozen=True)
class PartialOnlineBenchMetrics:
    """Métricas incrementais para cada 'yield' durante o stream."""

    step_latency_s: float
    memory_current_mb: float
    cpu_usage_percent: float
    input_points_indices: list[int] = field(default_factory=list)
    input_points_count: int = 0
    custom_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a flat dictionary for DataFrame inclusion."""
        d = {
            "step_latency_s": self.step_latency_s,
            "memory_current_mb": self.memory_current_mb,
            "cpu_usage_percent": self.cpu_usage_percent,
            "input_points_count": self.input_points_count,
        }
        for k, v in self.custom_metadata.items():
            d[f"meta_{k}"] = v
        return d


@dataclass(frozen=True)
class OnlineBenchMetrics:
    """Resumo consolidado de uma sessão completa de streaming."""

    total_execution_time_s: float
    avg_step_latency_s: float
    max_memory_peak_mb: float
    total_points_processed: int
    total_results_yielded: int
    partial_metrics: list[PartialOnlineBenchMetrics] = field(default_factory=list)
    custom_metadata: dict[str, Any] = field(default_factory=dict)

    def to_df(self) -> pd.DataFrame:
        """Export all partial metrics to a Pandas DataFrame, including summary stats in metadata."""
        rows = [m.to_dict() for m in self.partial_metrics]
        df = pd.DataFrame(rows)
        # Add summary as attributes to the DF for convenience
        df.attrs["total_execution_time_s"] = self.total_execution_time_s
        df.attrs["avg_step_latency_s"] = self.avg_step_latency_s
        df.attrs["max_memory_peak_mb"] = self.max_memory_peak_mb
        return df
