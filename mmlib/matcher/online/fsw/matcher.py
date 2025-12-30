import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
from typing import AsyncIterable, AsyncIterator, Final, override

from shapely import LineString, Point
from shapely.ops import substring

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result.offline import MatchResult
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory


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
        lookahead: int = 1,
    ) -> None:
        super().__init__()
        if window_size <= 0 or lookahead < 0:
            raise ValueError("window_size must be > 0 and lookahead must be >= 0.")

        self._matcher = matcher
        self._window_size = window_size
        self._lookahead = lookahead

        self._window_buffer: deque[GPSPoint] = deque(maxlen=self._window_size)
        # Results buffer (Match results) for Look-Ahead decision making
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
            self._window_results_buffer = deque()
            self._result = OnlineMatchResult(matcher_name=self.matcher_name)
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
            self._result._update_sent(point)
            self._window_buffer.append(point)

            snapshot = list(self._window_buffer)

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor, self._matcher.match, snapshot
            )

            self._window_results_buffer.append(result)

            if len(self._window_results_buffer) > self._lookahead:
                # 2. Aggregate results applying dual stitching
                results_snapshot = list(self._window_results_buffer)
                agg_result = await self._agg_results(results_snapshot)
                yield agg_result

        # Drain remaining windows
        while len(self._window_results_buffer) > 0:
            results_snapshot = list(self._window_results_buffer)
            agg_result = await self._agg_results(results_snapshot)
            yield agg_result
            self._window_results_buffer.popleft()

    async def _agg_results(
        self, results_snapshot: list[MatchResult]
    ) -> OnlineMatchResult:
        """
        Aggregate results from the buffered windows, applying dual stitching.
        """
        if not results_snapshot:
            return copy.deepcopy(self._result)

        geometries = [
            LineString([(lon, lat) for lat, lon in res.matched_points])
            for res in results_snapshot
        ]

        edges_ids = [res.edge_ids for res in results_snapshot]

        stitched_geometry = self._resolve_geometric_stitch(geometries)
        stitched_edges = self._resolve_edge_stitch(edges_ids)

        stiched_points = (
            [
                Coordinate(lon=lon, lat=lat)
                for (lon, lat) in list(stitched_geometry.coords)
            ]
            if stitched_geometry
            else []
        )

        self._result.matched_points.extend(stiched_points)
        self._result.edge_ids.extend(stitched_edges or [])

        return copy.deepcopy(self._result)

    # --- STITCHING LOGIC ---

    def _resolve_geometric_stitch(
        self, lines: list[LineString], dissolve=True
    ) -> LineString | None:
        # 1. Filtragem Inicial: Remove None e geometrias vazias (is_empty)
        if not lines:
            return None

        valid_lines = [line for line in lines if line is not None and not line.is_empty]

        if not valid_lines:
            return None

        # Começamos com a primeira janela válida
        accumulated = valid_lines[0]

        # Tolerância para gap vs overlap (aprox 11m em graus)
        GAP_THRESHOLD = 0.0001

        for i in range(1, len(valid_lines)):
            next_line = valid_lines[i]

            # Modo concatenação simples (debug ou performance máxima)
            if not dissolve:
                # Converte para lista de coordenadas para fundir
                coords = list(accumulated.coords) + list(next_line.coords)
                accumulated = LineString(coords)
                continue

            # --- LÓGICA DE PROJEÇÃO (SMART STITCH) ---

            last_point = Point(accumulated.coords[-1])

            # Projeta o fim da acumulada na nova linha
            split_dist = next_line.project(last_point)

            # Verifica a distância real para decidir entre Gap ou Overlap
            projected_point = next_line.interpolate(split_dist)
            dist_to_line = last_point.distance(projected_point)

            new_segment_coords = []

            # CASO GAP (Buraco grande): Conecta com reta simples
            if dist_to_line > GAP_THRESHOLD:
                new_segment_coords = list(next_line.coords)

            # CASO OVERLAP (Sobreposição): Corta o passado redundante
            else:
                # Se a projeção indica que a nova linha está toda "para trás", ignora ela
                if split_dist >= next_line.length:
                    continue

                # Corta do ponto de projeção até o fim
                segment = substring(
                    next_line, start_dist=split_dist, end_dist=next_line.length
                )
                new_segment_coords = list(segment.coords)

            # União manual
            if not new_segment_coords:
                continue

            current_coords = list(accumulated.coords)

            # Evita duplicar o vértice de junção se forem idênticos
            if current_coords[-1] == new_segment_coords[0]:
                new_segment_coords.pop(0)

            accumulated = LineString(current_coords + new_segment_coords)

        return accumulated

    def _resolve_edge_stitch(self, edges: list[list[str]]) -> list[str] | None:
        if not edges:
            return None

        final_edges = edges[0].copy()

        for edge_list in edges[1:]:
            cut_index = self._resolve_edge_overlap(final_edges, edge_list)
            final_edges.extend(edge_list[cut_index:])

        return final_edges

    @staticmethod
    def _resolve_edge_overlap(edges_a: list[str], edges_b: list[str]) -> int:
        """
        Finds where A ends for B to begin based on Edge IDs.
        """
        if not edges_b:
            return len(edges_a)

        # Look for the longest suffix of A that matches a prefix of B
        # Optimization: start search from the second half of A
        start_search = len(edges_a) // 2

        for i in reversed(range(start_search, len(edges_a))):
            if edges_a[i] == edges_b[0]:
                # Potential overlap found
                overlap_len = len(edges_a) - i
                if edges_a[i:] == edges_b[:overlap_len]:
                    return i  # Cut A here

        return len(edges_a)  # No overlap found


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)
