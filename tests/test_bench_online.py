import asyncio
from datetime import datetime
from typing import AsyncIterable, AsyncIterator
from mmlib.matcher.base import BaseOnlineMatcher
from mmlib.result import OnlineMatchResult
from mmlib.types import GPSPoint, Coordinate


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

    df = summary.to_df()
    print("\nSummary DataFrame (Partial Metrics):")
    print(df)

    assert summary.total_results_yielded == 3
    assert summary.total_points_processed == 3
    print("\nOnline benchmark test passed!")


if __name__ == "__main__":
    asyncio.run(test_online_bench())
