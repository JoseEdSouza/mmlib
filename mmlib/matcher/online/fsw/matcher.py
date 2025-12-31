import asyncio
import numpy as np
import osmnx as ox
import networkx as nx

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, Final, override

from scipy.interpolate import interp1d

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result.offline import MatchResult
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    _base_matcher_name: Final[str] = "FSW"

    def __init__(
        self,
        matcher: BaseMatcher,
        G_road: nx.MultiDiGraph,
        *,
        window_size: int = 100,
        lookahead: int = 1,
    ) -> None:
        super().__init__()
        self._matcher = matcher
        self._G_road = G_road
        self._nodes_gdf, self._edges_gdf = ox.graph_to_gdfs(self._G_road)

        if window_size <= 0 or lookahead < 0:
            raise ValueError(
                "window_size must be > 0 and lookahead must be >= 0."
            )
        self._window_size = window_size
        self._lookahead = lookahead

        # Buffers
        self._window_buffer: deque[GPSPoint] = deque(maxlen=self._window_size)
        self._window_results_buffer: deque[MatchResult] = deque(
            maxlen=self._lookahead + 1
        )
        self._result = OnlineMatchResult(matcher_name=self.matcher_name)

        self._executor: ThreadPoolExecutor | None = None

    @property
    @override
    def matcher_name(self) -> str:
        return f"{self._base_matcher_name}({self._matcher.matcher_name})"

    @override
    async def start(self) -> None:
        if not self._started:
            self._window_buffer = deque(maxlen=self._window_size)
            self._window_results_buffer = deque(maxlen=self._lookahead + 1)
            self._result = OnlineMatchResult(matcher_name=self.matcher_name)
            self._raw_input_overlap = []
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
        curr_idx: int = -1

        async for point in points:
            self._result._update_sent(point)
            self._window_buffer.append(point)

            if len(self._window_buffer) < self._window_size:
                continue

            window_snapshot = list(self._window_buffer)
            loop = asyncio.get_running_loop()
            match_result = await loop.run_in_executor(
                self._executor,
                self._matcher.match,
                window_snapshot,
            )
            self._window_results_buffer.append(match_result)
            if len(self._window_results_buffer) < self._lookahead:
                continue

            result, curr_idx = self._agg_result(curr_idx)
            yield result

        while len(self._window_results_buffer) > 0:
            result, curr_idx = self._agg_result(curr_idx)
            yield result
            self._window_results_buffer.popleft()

    def _agg_result(self, cut_idx: int) -> tuple[OnlineMatchResult, int]:
        results_snapshot = list(self._window_results_buffer)

        matched_edges = np.concatenate([res.edge_ids for res in results_snapshot])

        matched_edges = matched_edges[cut_idx + 1 :]

        stitched_edges = self._stitch_edges(
            edges_a=self._result.edge_ids,
            edges_b=matched_edges.tolist(),
        )

        new_coords = self._get_geometry_from_edges(matched_edges.tolist())
        smoothed_coords = self._smooth_geometry(
            traj_a=self._result.matched_points,
            traj_b=new_coords,
        )

        self._result.edge_ids = stitched_edges
        self._result.matched_points = smoothed_coords

        new_cut_idx = cut_idx + len(matched_edges)

        return self._result, new_cut_idx


    def _smooth_geometry(
        self, traj_a: list[Coordinate], traj_b: list[Coordinate]
    ) -> list[Coordinate]:

        ## trajectory smoothing interpolation
        # Combine trajectories
        all_coords = traj_a + traj_b
        if len(all_coords) < 2:
            return all_coords

        kind = "cubic" if len(all_coords) >= 4 else "linear"

        # Extract lat/lon arrays
        lats = np.array([c.lat for c in all_coords])
        lons = np.array([c.lon for c in all_coords])

        # Create parameter array for interpolation
        t = np.linspace(0, 1, len(all_coords))
        t_smooth = np.linspace(0, 1, len(all_coords) * 2)

        # Interpolate coordinates
        f_lat = interp1d(t, lats, kind=kind)
        f_lon = interp1d(t, lons, kind=kind)

        smoothed_lats = f_lat(t_smooth)
        smoothed_lons = f_lon(t_smooth)

        # Convert back to Coordinate objects
        smoothed_coords = [
            Coordinate(lat=lat, lon=lon)
            for lat, lon in zip(smoothed_lats, smoothed_lons)
        ]

        return smoothed_coords

    def _get_geometry_from_edges(self, edge_ids: list[str]) -> list[Coordinate]:
        edges_by_osm = self._edges_gdf.set_index("osmid")
        coords: list[Coordinate] = []
        for edge_id in edge_ids:
            if edge_id not in edges_by_osm.index:
                continue
            edge_geom = edges_by_osm.iloc[
                edges_by_osm.index == edge_id
            ].geometry.values[0]
            if edge_geom.geom_type == "LineString":
                coords.extend(
                    [Coordinate(lat=lat, lon=lon) for lon, lat in edge_geom.coords]
                )

        return coords

    def _stitch_edges(self, edges_a: list[str], edges_b: list[str]) -> list[str]:
        if not edges_b:
            return edges_a

        # Procura overlap de trás pra frente
        start_search = len(edges_a) // 2

        for i in range(len(edges_a) - 1, start_search - 1, -1):
            if edges_a[i] == edges_b[0]:
                overlap_len = len(edges_a) - i
                if edges_a[i:] == edges_b[:overlap_len]:
                    # Retorna A cortado + B inteiro
                    return edges_a[:i] + edges_b

        return edges_a + edges_b


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)
