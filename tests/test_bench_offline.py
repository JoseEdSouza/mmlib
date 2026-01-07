from datetime import datetime
from mmlib.matcher.base import BaseMatcher
from mmlib.result import MatchResult
from mmlib.types import GPSPoint, Coordinate


class MockMatcher(BaseMatcher):
    @property
    def matcher_name(self) -> str:
        return "mock"

    def match(self, points: list[GPSPoint]) -> MatchResult:
        import time

        time.sleep(0.5)  # Simulate work
        # Simulate some memory usage
        _data = [0] * 1_000_000
        return MatchResult(
            matcher_name=self.matcher_name,
            measurement_points=points,
            matched_points=[Coordinate(p.lat, p.lon) for p in points],
            edge_ids=["123"],
        )


def test_offline_bench():
    matcher = MockMatcher()
    points = [
        GPSPoint(-23.55, -46.63, datetime.now()),
        GPSPoint(-23.56, -46.64, datetime.now()),
    ]

    print("Running bench_match...")
    result, metrics = matcher.bench_match(points)

    print(f"Matcher: {result.matcher_name}")
    print(f"Metrics: {metrics}")

    df = metrics.to_df()
    print("\nMetrics DataFrame:")
    print(df)

    assert metrics.execution_time_ms >= 500
    assert metrics.memory_peak_mb > 0
    assert "run_id" in df.columns
    assert len(df["run_id"].iloc[0]) == 32
    print("\nOffline benchmark test passed!")


if __name__ == "__main__":
    test_offline_bench()
