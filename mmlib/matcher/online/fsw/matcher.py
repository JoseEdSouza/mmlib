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
        # 1. Filtros de Segurança (Empty/None)
        if not lines:
            return None
        valid_lines = [line for line in lines if line is not None and not line.is_empty]
        if not valid_lines:
            return None

        # Inicializa com a primeira janela
        accumulated = valid_lines[0]

        # CONSTANTES DE AJUSTE
        # Quantos pontos do final da acumulada usamos para comparar?
        # Não precisa comparar a rota inteira de 10km com a nova janela, só o finalzinho.
        LOOKBACK_SIZE = 20

        # Distância máxima para aceitar o match (em graus). ~50 metros
        MAX_MATCH_DIST = 0.0005

        for i in range(1, len(valid_lines)):
            new_line = valid_lines[i]

            # Se dissolve=False, apenas concatena (modo rápido/debug)
            if not dissolve:
                coords = list(accumulated.coords) + list(new_line.coords)
                accumulated = LineString(coords)
                continue

            # --- PREPARAÇÃO DOS DADOS PARA DTW ---
            # Converter para Numpy arrays (necessário para dtaidistance)
            # Pegamos apenas o "rabo" da acumulada para ganhar performance
            acc_coords = np.array(accumulated.coords)
            new_coords = np.array(new_line.coords)

            tail_start_idx = max(0, len(acc_coords) - LOOKBACK_SIZE)
            acc_tail = acc_coords[tail_start_idx:]

            # --- LÓGICA DTW (SMART ALIGNMENT) ---

            # O warping_path retorna uma lista de tuplas [(idx_tail, idx_new), ...]
            # que alinham os pontos de melhor forma possível.
            path = cast(list[tuple[int, int]], dtw_ndim.warping_path(acc_tail, new_coords))

            # Queremos saber: Onde, na nova linha, termina a minha linha acumulada?
            # Procuramos o último ponto da nossa tail no path.
            last_tail_idx = len(acc_tail) - 1

            # Encontrar o índice correspondente na nova janela (match_idx)
            # Iteramos de trás para frente no path para achar o último alinhamento
            match_idx_in_new = 0
            dist_between_match = float("inf")

            found_match = False
            for p_tail, p_new in reversed(path):
                if p_tail == last_tail_idx:
                    match_idx_in_new = p_new

                    # Calcular a distância real entre os pontos "casados"
                    # para saber se é um overlap válido ou um gap (túnel/perda de sinal)
                    p1 = acc_tail[p_tail]
                    p2 = new_coords[p_new]
                    dist_between_match = np.linalg.norm(p1 - p2)

                    found_match = True
                    break

            # --- DECISÃO DE CORTE ---

            new_segment_points = []

            if not found_match or dist_between_match > MAX_MATCH_DIST:
                # CASO GAP: O DTW não achou um bom par ou os pontos estão muito longe.
                # Assumimos que o carro pulou/perdeu sinal. Adiciona tudo e conecta com reta.
                new_segment_points = new_coords
            else:
                # CASO OVERLAP: Achamos onde a antiga termina dentro da nova.
                # Pegamos tudo o que vem DEPOIS desse ponto na nova janela.
                # match_idx_in_new é o ponto que JA TEMOS. Queremos o próximo.
                start_cut = match_idx_in_new + 1

                if start_cut < len(new_coords):
                    new_segment_points = new_coords[start_cut:]
                else:
                    # A nova janela está inteiramente contida no passado (backtracking)
                    # Não adicionamos nada.
                    new_segment_points = []

            # --- UNIÃO ---
            if len(new_segment_points) > 0:
                # Converter de volta para list of tuples para o Shapely
                points_to_add = [tuple(p) for p in new_segment_points]

                # Construir nova geometria
                # Nota: acc_coords já é a lista completa da acumulada
                final_coords = list(acc_coords) + points_to_add
                accumulated = LineString(final_coords)

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
