# Benchmarking Strategy - Offline Mode

This document details the implementation of performance metrics for matchers operating in batch (Offline) mode, based on `BaseMatcher`.

## 1. Data Structure

An immutable dataclass is used to consolidate metrics for a complete execution.

```python
from mmlib.benchmark import BenchMetrics
```

The structure contains:

- `execution_time_s`: Total execution time (flight time).
- `memory_peak_mb`: Peak RAM memory detected.
- `cpu_usage_percent`: Average CPU usage during the task.
- `custom_metadata`: Matcher-specific metadata.

## 2. Matcher API

The `BaseMatcher` class (in `mmlib.matcher.base`) has been extended with the method:

```python
def bench_match(self, points: list[GPSPoint]) -> tuple[MatchResult, BenchMetrics]:
    """
    Executes matching while collecting global performance metrics.
    """
```

## 3. Implementation via BenchmarkMixin

`BenchmarkMixin` is located in `mmlib.benchmark` and provides utilities for resource capture using `psutil` and `time.perf_counter`.

---

## 4. Technical Considerations

- **Isolation**: Offline benchmarking is synchronous, facilitating precise CPU time measurement.
- **Overhead**: Measurement cost is negligible compared to the geometric/network processing of matchers.

---

*Architecture updated according to benchmark module refactoring.*
