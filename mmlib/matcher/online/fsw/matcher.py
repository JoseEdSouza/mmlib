import asyncio
import copy
import numpy as np

from collections import deque
from concurrent.futures import ThreadPoolExecutor

from typing import AsyncIterable, AsyncIterator, Final, cast, override

from shapely import LineString, Point
from shapely.ops import linemerge

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
        Une uma lista de LineStrings usando algoritmo de Overlap Geométrico
        baseado em Reamostragem + RMSE para garantir continuidade visual.
        """
        if not lines:
            return None

        # Se só tem uma linha ou não devemos dissolver, retorna o merge simples
        if len(lines) == 1:
            return lines[0]

        if not dissolve:
            # Apenas une as linhas sem processar o overlap inteligente
            # (Nota: linemerge pode falhar se não tocarem, então fallback para união)
            try:
                return cast(LineString, linemerge(lines))
            except:
                # Fallback: cria uma multilinestring ou une coordenadas brutas
                all_coords = []
                for line in lines:
                    all_coords.extend(list(line.coords))
                return LineString(all_coords)

        # --- ALGORITMO DE COSTURA ITERATIVA ---

        # Começamos com a primeira geometria consolidada
        merged_line = lines[0]

        # Iteramos pelas próximas janelas tentando costurar uma a uma
        for next_line in lines[1:]:
            merged_line = self._stitch_two_geometries(merged_line, next_line)

        return merged_line

    def _stitch_two_geometries(
        self, geom_a: LineString, geom_b: LineString
    ) -> LineString:
        """
        Encontra o ponto de corte em geom_a onde geom_b começa e funde as duas.
        """
        # Constantes do Algoritmo (podem virar atributos da classe)
        SAMPLE_STEP = 5.0  # metros
        TEST_LENGTH = 20.0  # metros
        RMSE_THRESHOLD = 15.0  # metros

        if geom_a.is_empty:
            return geom_b
        if geom_b.is_empty:
            return geom_a

        # 1. Cria a assinatura do INÍCIO da nova geometria (B)
        # Reamostra os primeiros 20 metros de B
        signature_b = self._resample_shapely(geom_b, SAMPLE_STEP, TEST_LENGTH)

        if not signature_b:
            # B é muito curta ou inválida, apenas anexa
            coords = list(geom_a.coords) + list(geom_b.coords)
            return LineString(coords)

        coords_a = list(geom_a.coords)
        len_a = len(coords_a)

        # 2. Busca essa assinatura no FINAL da geometria atual (A)
        # Varre os últimos 40% dos vértices de A como candidatos a corte
        start_search_idx = int(len_a * 0.6)

        best_cut_index = -1
        min_rmse = float("inf")

        # Itera de trás para frente para achar o maior overlap possível (Greedy)
        for i in range(len_a - 1, start_search_idx, -1):

            # Constrói o segmento candidato (de i até o fim de A)
            # Precisamos converter para LineString para usar métodos de geometria
            candidate_coords = coords_a[i:]
            if len(candidate_coords) < 2:
                continue

            candidate_line = LineString(candidate_coords)

            # Se o candidato for muito mais curto que a assinatura,
            # o RMSE vai falhar ou ser impreciso.
            if candidate_line.length < (TEST_LENGTH * 0.5):
                continue

            # Reamostra o candidato de A
            signature_a = self._resample_shapely(
                candidate_line, SAMPLE_STEP, TEST_LENGTH
            )

            # Calcula o erro
            current_rmse = self._calculate_rmse_shapely(signature_a, signature_b)

            if current_rmse < min_rmse:
                min_rmse = current_rmse
                best_cut_index = i

        # 3. Aplica a Costura
        if min_rmse < RMSE_THRESHOLD and best_cut_index != -1:
            # SUCESSO: Cortamos A no índice encontrado e colamos B inteiro
            # coords_a[:best_cut_index+1] inclui o ponto de solda
            # coords_b[1:] evita duplicar o ponto se eles forem idênticos no espaço

            final_coords_a = coords_a[: best_cut_index + 1]
            final_coords_b = list(geom_b.coords)

            # Pequena verificação para não duplicar vértice exato
            if self._dist_sq(final_coords_a[-1], final_coords_b[0]) < 1e-6:
                final_coords = final_coords_a + final_coords_b[1:]
            else:
                final_coords = final_coords_a + final_coords_b

            return LineString(final_coords)

        else:
            # FALHA: Não convergiu. Retorna A + B (Gap Filling / Linha reta)
            return LineString(coords_a + list(geom_b.coords))

    def _resample_shapely(
        self, line: LineString, step: float, max_len: float
    ) -> list[Point]:
        """Gera pontos interpolados ao longo da LineString."""
        points = []
        current_dist = 0.0
        total_length = line.length
        limit = min(total_length, max_len)

        while current_dist <= limit:
            points.append(line.interpolate(current_dist))
            current_dist += step

        return points

    def _calculate_rmse_shapely(self, pts_a: list[Point], pts_b: list[Point]) -> float:
        """Calcula RMSE entre duas listas de Pontos Shapely."""
        n = min(len(pts_a), len(pts_b))
        if n < 2:
            return float("inf")

        coords_a = np.array([(p.x, p.y) for p in pts_a[:n]])
        coords_b = np.array([(p.x, p.y) for p in pts_b[:n]])
        
        sum_sq = np.sum((coords_a - coords_b) ** 2)

        return np.sqrt(sum_sq / n)

    def _dist_sq(self, c1, c2):
        """Distância quadrática simples entre tuplas de coords (x, y)"""
        dx = c1[0] - c2[0]
        dy = c1[1] - c2[1]
        return dx * dx + dy * dy

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
