import asyncio
from datetime import datetime
from typing import AsyncIterable, AsyncIterator
from mmlib.matcher.base import BaseOnlineMatcher
from mmlib.result import OnlineMatchResult
from mmlib.types import GPSPoint, Coordinate
import pytest


class MockOnlineMatcher(BaseOnlineMatcher):
    @property
    def matcher_name(self) -> str:
        return "mock_online"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        result = OnlineMatchResult(matcher_name=self.matcher_name)

        async for p in points:
            await asyncio.sleep(0.1)  # Simulate network/processing delay
            result._update_sent(p)
            result._update_matched(Coordinate(p.lat + 0.001, p.lon + 0.001))
            yield result


@pytest.mark.asyncio
async def test_online_bench():
    matcher = MockOnlineMatcher()
    points = [
        GPSPoint(-23.55, -46.63, datetime.now()),
        GPSPoint(-23.56, -46.64, datetime.now()),
        GPSPoint(-23.57, -46.65, datetime.now()),
    ]

    print("--- Testing bench_match_stream ---")

    async def point_gen():
        for p in points:
            yield p

    count = 0
    async for result, partial in matcher.bench_match_stream(point_gen()):
        count += 1
        print(f"Yield {count}:")
        print(f"  Result matched points: {len(result.matched_points)}")
        print(f"  Step Latency: {partial.step_latency_ms:.2f}ms")
        print(f"  Input Indices: {partial.input_points_indices}")
        assert partial.step_latency_ms >= 100
        assert len(partial.input_points_indices) > 0

    print("\n--- Testing bench_match_batch ---")
    res, summary = await matcher.bench_match_batch(points)
    print(f"Summary total time: {summary.total_execution_time_ms:.2f}ms")
    print(f"Avg latency: {summary.avg_step_latency_ms:.2f}ms")
    print(f"Total results: {summary.total_results_yielded}")

    print("\n--- Testing to_df() with new columns ---")
    df = summary.to_df(expand_summary=True)
    print("DataFrame columns:", df.columns.tolist())

    # Verify mandatory columns
    expected_cols = [
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
        "cpu_time_ms",
    ]
    for col in expected_cols:
        assert col in df.columns, f"Column {col} missing from DataFrame"

    # Verify dropped columns
    assert "input_points_count" not in df.columns
    assert not any(c.startswith("meta_") for c in df.columns)

    # Verify values
    assert df["step_index"].tolist() == [0, 1, 2]
    assert df["points_per_step"].tolist() == [1, 1, 1]
    assert df["cum_points"].tolist() == [1, 2, 3]

    # First inter_arrival_ms should be NA (or pd.NA)
    import pandas as pd

    assert pd.isna(df.loc[0, "inter_arrival_ms"])

    # Verify attrs
    assert df.attrs["total_results_yielded"] == 3
    assert df.attrs["total_points_processed"] == 3
    assert df.attrs["matcher_name"] == "mock_online"
    assert df.attrs["mode"] == "online"
    assert "throughput_in_pps" in df.attrs

    print("\nOnline benchmark verification passed!")


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_online_bench())
