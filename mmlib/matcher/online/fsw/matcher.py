import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, Final, override

import numpy as np
from geopy.distance import great_circle

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result.offline import MatchResult
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory


def haversine_distance(p1: Coordinate, p2: Coordinate) -> float:
    """
    Calculates the Haversine distance in meters between two GPS coordinates.
    Uses geopy's great_circle for a standard implementation.
    """
    return great_circle((p1.lat, p1.lon), (p2.lat, p2.lon)).meters


def interpolate_point(p1: Coordinate, p2: Coordinate, fraction: float) -> Coordinate:
    """
    Performs simple linear interpolation between two Lat/Lon points.
    """
    new_lat = p1.lat + (p2.lat - p1.lat) * fraction
    new_lon = p1.lon + (p2.lon - p1.lon) * fraction
    return Coordinate(lat=new_lat, lon=new_lon)


def resample_polyline(
    points: list[Coordinate], step: float, max_len: float
) -> list[Coordinate]:
    """
    Resamples a polyline at a fixed 'step' interval up to 'max_len'.
    """
    if not points:
        return []

    resampled = [points[0]]
    current_dist_acc = 0.0
    target_dist = step

    for i in range(len(points) - 1):
        p1, p2 = points[i], points[i + 1]
        seg_len = haversine_distance(p1, p2)

        while target_dist <= current_dist_acc + seg_len:
            if target_dist > max_len:
                return resampled

            fraction = (target_dist - current_dist_acc) / seg_len if seg_len > 0 else 0
            new_p = interpolate_point(p1, p2, fraction)
            resampled.append(new_p)
            target_dist += step

        current_dist_acc += seg_len
        if current_dist_acc >= max_len:
            break

    return resampled


def calculate_rmse(pts_a: list[Coordinate], pts_b: list[Coordinate]) -> float:
    """
    Calculates the Root Mean Square Error (RMSE) between two lists of points.
    Uses numpy for efficient calculation.
    """
    n = min(len(pts_a), len(pts_b))
    if n < 2:
        return float("inf")

    # Compute distances between corresponding points
    distances = np.array(
        [haversine_distance(pts_a[i], pts_b[i]) for i in range(n)]
    )
    
    # Calculate RMSE using numpy
    return float(np.sqrt(np.mean(distances**2)))


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    """
    Online matcher using Fixed Sliding Window (FSW) with Look-Ahead Stitching.
    Implements Dual Stitching: Topological (Edges) and Geometric (Resampling + RMSE).
    """

    _base_matcher_name: Final[str] = "FSW"

    def __init__(
        self,
        matcher: BaseMatcher,
        *,
        window_size: int = 100,
        overlap_size: int = 20,
        lookahead: int = 1,
        sampling_step: float = 5.0,
        test_length: float = 20.0,
        rmse_threshold: float = 15.0,
    ) -> None:
        super().__init__()
        if window_size <= 0 or overlap_size >= window_size:
            raise ValueError("Invalid window/overlap configuration.")

        self._matcher = matcher
        self._window_size = window_size
        self._overlap_size = overlap_size
        self._lookahead = lookahead
        
        # Hyperparameters
        self._sampling_step = sampling_step
        self._test_length = test_length
        self._rmse_threshold = rmse_threshold

        # Input buffer (Raw Points) to form overlapping windows
        self._raw_buffer: list[GPSPoint] = []

        # Results buffer (Match results) for Look-Ahead decision making
        self._window_results_buffer: deque[MatchResult] = deque()

        # Accumulated result
        self._result = OnlineMatchResult(matcher_name=self.matcher_name)

        # ThreadPoolExecutor to run offline matching without blocking the event loop
        self._executor: ThreadPoolExecutor | None = None

    @property
    @override
    def matcher_name(self) -> str:
        return f"{self._base_matcher_name}({self._matcher.matcher_name})"

    @override
    async def start(self) -> None:
        if not self._started:
            self._started = True
            self._executor = ThreadPoolExecutor(max_workers=1)

    @override
    async def stop(self) -> None:
        if self._started and self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None
            self._started = False

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        await self.start()

        async for point in points:
            # 1. Accumulate raw points and update input tracking
            self._raw_buffer.append(point)
            self._result._update_sent(point)

            # 2. Trigger offline match once the window is full
            if len(self._raw_buffer) >= self._window_size:
                await self._process_current_raw_batch()

            # 3. If enough windows are buffered, attempt to stitch and emit
            while len(self._window_results_buffer) > self._lookahead:
                yield await self._stitch_and_emit()

        # 4. Flush: process any remaining points in the raw buffer
        if len(self._raw_buffer) > 0:
            await self._process_current_raw_batch()

        # 5. Flush: emit remaining results
        while len(self._window_results_buffer) > 0:
            yield await self._emit_remaining()

    async def _process_current_raw_batch(self):
        """
        Sends the current batch to the offline matcher and manages overlap for the next window.
        """
        if not self._executor:
            return

        # Snapshot for the thread
        batch_snapshot = list(self._raw_buffer)

        # Execute Offline Match (Blocking) in a separate thread
        loop = asyncio.get_running_loop()
        match_result = await loop.run_in_executor(
            self._executor, self._matcher.match, batch_snapshot
        )

        self._window_results_buffer.append(match_result)

        # Prepare raw_buffer for the next window by keeping the last 'overlap_size' points
        if self._overlap_size > 0:
            self._raw_buffer = self._raw_buffer[-self._overlap_size :]
        else:
            self._raw_buffer = []

    async def _stitch_and_emit(self) -> OnlineMatchResult:
        """
        Attempts to stitch the current window (0) with the future window (1) and emits the result.
        """

        current_res = self._window_results_buffer[0]
        future_res = self._window_results_buffer[1]

        # --- 1. Topological Overlap (Edges) ---
        edges_cut_idx = self._resolve_edge_overlap(
            current_res.edge_ids, future_res.edge_ids
        )
        final_edges = current_res.edge_ids[:edges_cut_idx]

        # --- 2. Geometric Overlap (Points) ---
        geom_cut_idx = self._resolve_geometry_overlap(
            current_res.matched_points, future_res.matched_points
        )
        final_points = current_res.matched_points[:geom_cut_idx]

        # --- 3. Update state and emit ---
        self._append_to_result(final_edges, final_points)

        # Remove processed window; the future becomes the new current
        self._window_results_buffer.popleft()

        return self._result

    async def _emit_remaining(self) -> OnlineMatchResult:
        """
        Emits the remaining window without stitching (end of stream).
        """
        res = self._window_results_buffer.popleft()
        self._append_to_result(res.edge_ids, res.matched_points)
        return self._result

    def _append_to_result(self, new_edges: list, new_points: list[Coordinate]):
        """
        Appends data to the accumulated result, avoiding simple duplicates at boundaries.
        """

        # Append Edges (avoiding duplicate if the last edge matches the first new edge)
        if (
            self._result.edge_ids
            and new_edges
            and self._result.edge_ids[-1] == new_edges[0]
        ):
            self._result.edge_ids.extend(new_edges[1:])
        else:
            self._result.edge_ids.extend(new_edges)

        # Append Points (Geometry)
        self._result.matched_points.extend(new_points)

    # --- STITCHING LOGIC ---

    def _resolve_edge_overlap(self, edges_a: list, edges_b: list) -> int:
        """
        Finds where A ends for B to begin based on Edge IDs.
        """
        if not edges_b:
            return len(edges_a)

        # Look for the longest suffix of A that matches a prefix of B
        # Optimization: start search from the second half of A
        start_search = len(edges_a) // 2

        for i in range(len(edges_a) - 1, start_search - 1, -1):
            if edges_a[i] == edges_b[0]:
                # Potential overlap found
                overlap_len = len(edges_a) - i
                if edges_a[i:] == edges_b[:overlap_len]:
                    return i  # Cut A here

        return len(edges_a)  # No overlap found

    def _resolve_geometry_overlap(
        self, geom_a: list[Coordinate], geom_b: list[Coordinate]
    ) -> int:
        """
        Finds the geometric cut point using resampling and RMSE.
        """
        if not geom_b or not geom_a:
            return len(geom_a)

        # 1. Create a signature for the START of B (using hyperparameters)
        sig_b = resample_polyline(
            geom_b, self._sampling_step, self._test_length
        )
        if not sig_b:
            return len(geom_a)

        best_cut_idx = len(geom_a)
        min_rmse = float("inf")

        # 2. Search for this signature at the END of A
        start_search = int(len(geom_a) * 0.6)

        for i in range(start_search, len(geom_a)):
            # Segment in A starting at index i
            candidate_segment = geom_a[i:]

            # Resample candidate A segment
            sig_a = resample_polyline(
                candidate_segment, self._sampling_step, self._test_length
            )

            # Compare using RMSE
            rmse = calculate_rmse(sig_a, sig_b)

            if rmse < min_rmse:
                min_rmse = rmse
                best_cut_idx = i

        # 3. Validation
        if min_rmse < self._rmse_threshold:
            return best_cut_idx

        return len(geom_a)  # RMSE too high, return all of A


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)