# Benchmarking Strategy - Online Mode

This document details the implementation of performance metrics for matchers operating in streaming (Online) mode, based on `BaseOnlineMatcher`.

## 1. Data Structures

Online mode requires two levels of metrics due to the incremental nature of processing, both located in `mmlib.benchmark`.

### 1.1 Partial Metrics (Incremental)

These are sent with every `yield` of the stream to monitor latency per step.

```python
from mmlib.benchmark import PartialOnlineBenchMetrics
```

### 1.2 Consolidated Metrics (Total)

Final summary after the stream closes.

```python
from mmlib.benchmark import OnlineBenchMetrics
```

## 2. Online Matcher API

The `BaseOnlineMatcher` class is extended with two main methods:

```python
def bench_match_stream(
    self, points: AsyncIterable[GPSPoint]
) -> AsyncIterator[tuple[OnlineMatchResult, PartialOnlineBenchMetrics]]:
    """
    Executes matching in stream, emitting partial metrics.
    """

async def bench_match_batch(
    self, points: list[GPSPoint]
) -> tuple[OnlineMatchResult, OnlineBenchMetrics]:
    """
    Executes total matching on a list of points and returns a consolidated summary.
    """
```

## 3. N:M Traceability

It is mandatory for `PartialOnlineBenchMetrics` to report which input points generated the result via `input_points_indices`. This allows calculating the real end-to-end latency even in matchers with result amortization (such as FSW).

---

## 4. Implementation via BenchmarkMixin

The mixin in `mmlib.benchmark` manages asynchronous collection to ensure that measurement does not block the event loop.

---

*Architecture updated according to benchmark module refactoring.*
