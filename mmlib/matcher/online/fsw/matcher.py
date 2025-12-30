import asyncio
import copy
import numpy as np

from collections import deque
from concurrent.futures import ThreadPoolExecutor

from typing import AsyncIterable, AsyncIterator, Final, cast, override

from dtaidistance import dtw_ndim
from shapely import LineString

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
        """
        Costura uma lista sequencial de LineStrings em uma única LineString.
        
        Estratégia:
        1. Mantém a ordem da lista (respeita o fluxo temporal).
        2. Remove pontos duplicados na junção (onde o fim de A == início de B).
        3. Preenche gaps automaticamente (se fim de A != início de B, cria uma reta).
        """
        
        if not lines:
            return None

        # Se só tem uma linha, não há o que costurar
        if len(lines) == 1:
            return lines[0]

        # Se dissolve=False, teoricamente retornaríamos um MultiLineString, 
        # mas a assinatura da função pede LineString. 
        # Assumimos que o objetivo é sempre unificar a trajetória.

        # --- Abordagem de Concatenação de Coordenadas (Mais Robusta para Trajetórias) ---
        # Ao contrário de ops.linemerge, isso garante a ordem temporal dos pontos.
        
        merged_coords = []
        
        for i, line in enumerate(lines):
            if line.is_empty:
                continue
                
            current_coords = list(line.coords)
            
            if i == 0:
                merged_coords.extend(current_coords)
            else:
                # Verifica a "solda" com o segmento anterior
                last_point = merged_coords[-1]
                first_point_new = current_coords[0]
                
                if last_point == first_point_new:
                    # Perfeito: O fim da anterior é exatamente o início desta.
                    # Adicionamos a partir do segundo ponto para evitar duplicata.
                    merged_coords.extend(current_coords[1:])
                else:
                    # Gap (Buraco) ou Salto: 
                    # O algoritmo de corte anterior deixou um espaço ou as janelas não se tocaram.
                    # Simplesmente adicionamos os novos pontos. 
                    # O Shapely criará automaticamente uma linha reta (gap filling) entre
                    # last_point e first_point_new ao criar o LineString final.
                    merged_coords.extend(current_coords)

        # Validação final: precisa de pelo menos 2 pontos para formar uma linha
        if len(merged_coords) < 2:
            return None

        return LineString(merged_coords)

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
