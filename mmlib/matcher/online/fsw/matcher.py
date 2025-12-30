import asyncio
import copy
import numpy as np
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, Final, override

from geopy.distance import geodesic

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result.offline import MatchResult
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory

type Float64NDArray = np.ndarray[tuple[int, int], np.dtype[np.float64]]


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    _base_matcher_name: Final[str] = "FSW"

    def __init__(
        self,
        matcher: BaseMatcher,
        *,
        window_size: int = 100,
        lookahead: int = 1,
    ) -> None:
        super().__init__()
        self._matcher = matcher
        self._window_size = window_size
        self._lookahead = lookahead

        # Buffers
        self._window_buffer: deque[GPSPoint] = deque(maxlen=self._window_size)
        self._window_results_buffer: deque[MatchResult] = deque(
            maxlen=self._lookahead + 1
        )
        self._result = OnlineMatchResult(matcher_name=self.matcher_name)

        # Overlap de entrada (Raw)
        self._raw_input_overlap: list[GPSPoint] = []
        self._input_overlap_size = 20

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

        batch_buffer = []

        async for point in points:
            self._result._update_sent(point)
            batch_buffer.append(point)

            # Lógica de enchimento da janela
            if len(batch_buffer) + len(self._raw_input_overlap) >= self._window_size:
                full_window = self._raw_input_overlap + batch_buffer

                # Offload para thread (Matcher é CPU bound)
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    self._executor, self._matcher.match, full_window
                )

                self._window_results_buffer.append(result)

                # Atualiza overlap de entrada
                self._raw_input_overlap = full_window[-self._input_overlap_size :]
                batch_buffer = []

                # Verifica se pode costurar (Lookahead)
                if len(self._window_results_buffer) > self._lookahead:
                    agg_result = await self._process_head_of_buffer()
                    if agg_result:
                        yield agg_result

        # Drena buffer final
        while len(self._window_results_buffer) > 0:
            agg_result = await self._process_head_of_buffer()
            if agg_result:
                yield agg_result

    async def _process_head_of_buffer(self) -> OnlineMatchResult | None:
        if not self._window_results_buffer:
            return None

        current = self._window_results_buffer[0]

        if len(self._window_results_buffer) > 1:
            future = self._window_results_buffer[1]

            # --- STITCHING GEOMÉTRICO OTIMIZADO ---
            # Converte para numpy array (N, 2) -> [[lat, lon], ...]
            # Usando numpy desde o início para evitar conversões repetidas
            pts_a_np = self._to_numpy(current.matched_points)
            pts_b_np = self._to_numpy(future.matched_points)

            final_geom_np = self._stitch_geometry_numpy(pts_a_np, pts_b_np)

            # --- STITCHING TOPOLÓGICO ---
            final_edges = self._stitch_edges(current.edge_ids, future.edge_ids)

            # Reconverte numpy -> Coordinate objects para o output
            final_geom_objs = [Coordinate(lon=p[1], lat=p[0]) for p in final_geom_np]

            self._result.matched_points.extend(final_geom_objs)

            # Merge inteligente de edges
            if (
                self._result.edge_ids
                and final_edges
                and self._result.edge_ids[-1] == final_edges[0]
            ):
                self._result.edge_ids.extend(final_edges[1:])
            else:
                self._result.edge_ids.extend(final_edges)

        else:
            # Janela final (sem lookahead)
            coords = [
                Coordinate(lon=lon, lat=lat) for lat, lon in current.matched_points
            ]
            self._result.matched_points.extend(coords)
            self._result.edge_ids.extend(current.edge_ids)

        self._window_results_buffer.popleft()
        return copy.deepcopy(self._result)

    # --- MATH & GEOMETRY (NUMPY + GEOPY) ---

    def _to_numpy[T](self, points: list[Coordinate]) -> Float64NDArray:
        """Converte lista de (lat, lon) para array numpy float64."""
        if not points:
            return np.empty((0, 2), dtype=np.float64)
        return np.array(points, dtype=np.float64)

    def _stitch_geometry_numpy(
        self, pts_a: Float64NDArray, pts_b: Float64NDArray
    ) -> Float64NDArray:
        """
        Versão vetorizada do Stitching Geométrico.
        pts_a, pts_b: Arrays (N, 2) formato [[lat, lon], ...]
        """
        if pts_a.size == 0:
            return pts_b
        if pts_b.size == 0:
            return pts_a

        SAMPLE_STEP = 5.0  # metros
        TEST_LENGTH = 20.0  # metros
        RMSE_THRESHOLD = 15.0  # metros

        # 1. Assinatura de B (usando Geopy para precisão de distância no resampling)
        sig_b = self._resample_numpy(pts_b, SAMPLE_STEP, TEST_LENGTH)

        best_cut_idx = len(pts_a)
        min_rmse = float("inf")

        # 2. Varredura Otimizada
        # Só procura nos últimos 50% de A para ganhar performance
        start_search = int(len(pts_a) * 0.5)

        for i in range(start_search, len(pts_a)):
            # Slice numpy (visão, sem cópia profunda imediata)
            segment_a = pts_a[i:]

            # Se o segmento for muito curto, ignora (evita erro de dimensão)
            # Estimativa rápida: 1 grau ~= 111km. 20m ~= 0.00018 graus.
            if len(segment_a) < 2:
                continue

            # Reamostra candidato
            sig_a = self._resample_numpy(segment_a, SAMPLE_STEP, TEST_LENGTH)

            # RMSE Vetorizado
            rmse = self._calc_rmse_vectorized(sig_a, sig_b)

            if rmse < min_rmse:
                min_rmse = rmse
                best_cut_idx = i

        # 3. Decisão
        if min_rmse < RMSE_THRESHOLD:
            # Concatena A[:corte] + B
            # vstack é eficiente para juntar arrays
            return np.vstack((pts_a[:best_cut_idx], pts_b))
        else:
            # Fallback
            return np.vstack((pts_a, pts_b))

    def _resample_numpy(
        self, points: Float64NDArray, step_m: float, max_len_m: float
    ) -> Float64NDArray:
        """
        Reamostragem vetorizada.
        Usa Geopy para calcular o comprimento total real dos segmentos,
        mas usa interpolação linear vetorial (Numpy) para criar os pontos.
        """
        if len(points) < 2:
            return points

        # Calcula distâncias entre pontos consecutivos
        # Infelizmente Geopy é escalar, então iteramos para montar o array de distâncias reais.
        # Para vetores pequenos (<100 pts) isso é rápido o suficiente.
        # Otimização: Se performance for crítica, usar Haversine numpy aqui, mas Geopy é mais preciso (elipsoide).

        # Opção Híbrida: Iterar calculando dists com Geopy
        dists = np.zeros(len(points) - 1)
        for i in range(len(points) - 1):
            # geodesic((lat1, lon1), (lat2, lon2)).meters
            dists[i] = geodesic(points[i], points[i + 1]).meters

        # Distâncias acumuladas: [0, d1, d1+d2, ...]
        cum_dist = np.concatenate(([0], np.cumsum(dists)))

        # Define os alvos: [0, 5, 10, 15, 20] (limitado ao tamanho real da linha ou max_len)
        limit = min(cum_dist[-1], max_len_m)
        target_dists = np.arange(
            0, limit + 0.001, step_m
        )  # +0.001 para incluir borda se exato

        if len(target_dists) == 0:
            return points[:1]

        # np.searchsorted encontra onde cada target_dist se encaixa no array cum_dist
        # Retorna índices tal que: cum_dist[idx-1] <= target < cum_dist[idx]
        indices = np.searchsorted(cum_dist, target_dists) - 1

        # Clip para evitar index out of bounds no último ponto
        indices = np.clip(indices, 0, len(points) - 2)

        # Vetorização da interpolação
        # P1 = pontos[indices], P2 = pontos[indices+1]
        p1 = points[indices]
        p2 = points[indices + 1]

        dist_start = cum_dist[indices]
        dist_end = cum_dist[indices + 1]
        segment_lengths = dist_end - dist_start

        # Evita divisão por zero
        segment_lengths[segment_lengths == 0] = 1.0

        fractions = (target_dists - dist_start) / segment_lengths
        fractions = fractions[
            :, np.newaxis
        ]  # Transforma em coluna para multiplicar (N, 1)

        # Fórmula: P_new = P1 + (P2 - P1) * fraction
        interpolated = p1 + (p2 - p1) * fractions

        return interpolated

    def _calc_rmse_vectorized(self, pts_a: np.ndarray, pts_b: np.ndarray) -> float:
        """Calcula RMSE usando Haversine totalmente vetorizado no Numpy."""
        n = min(len(pts_a), len(pts_b))
        if n < 2:
            return float("inf")

        # Corta para o mesmo tamanho
        a = pts_a[:n]
        b = pts_b[:n]

        # Calcula distâncias ao quadrado
        dists_sq = self._haversine_vectorized_sq(a[:, 0], a[:, 1], b[:, 0], b[:, 1])

        return np.sqrt(np.mean(dists_sq))

    def _haversine_vectorized_sq(self, lat1, lon1, lat2, lon2):
        """
        Implementação Numpy do Haversine (retorna Distância^2 para economizar um sqrt antes da média).
        Input: Arrays numpy de latitudes e longitudes.
        Output: Array de (metros^2).
        """
        R = 6371000.0  # Raio da terra em metros

        phi1, phi2 = np.radians(lat1), np.radians(lat2)
        dphi = np.radians(lat2 - lat1)
        dlambda = np.radians(lon2 - lon1)

        a = (
            np.sin(dphi / 2) ** 2
            + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2) ** 2
        )

        # Distância exata: c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
        # Para pequenas distâncias, 2*asin(sqrt(a)) é seguro.
        c = 2 * np.arcsin(np.sqrt(a))

        d = R * c
        return d**2

    # --- EDGES (Mantido Lógica Python pura pois são Strings/Ints) ---

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
