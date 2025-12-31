import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, Final, override

import numpy as np

from geopy.distance import geodesic
from shapely.geometry import LineString

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result.offline import MatchResult
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory


# --- CONFIGURAÇÕES DO ALGORITMO ---
PASSO_AMOSTRAGEM_METROS = 5.0
COMPRIMENTO_TESTE_METROS = 20.0
LIMIAR_RMSE_METROS = 15.0
RAIO_TERRA_KM = 6371.0

# --- FUNÇÕES AUXILIARES DE GEOMETRIA (MATH HELPERS) ---


def haversine_distance(p1: Coordinate, p2: Coordinate) -> float:
    """Calcula distância em metros entre dois pontos GPS usando geodésica."""

    return geodesic((p1.lat, p1.lon), (p2.lat, p2.lon)).meters


def interpolate_point(p1: Coordinate, p2: Coordinate, fraction: float) -> Coordinate:
    """Interpolação linear entre Lat/Lon usando Shapely."""
    line = LineString([(p1.lon, p1.lat), (p2.lon, p2.lat)])
    distance = line.length * fraction
    point = line.interpolate(distance)
    return Coordinate(lat=point.y, lon=point.x)


def resample_polyline(
    points: list[Coordinate], step: float, max_len: float
) -> list[Coordinate]:
    """Reamostra a polilinha a cada 'step' metros até atingir 'max_len'."""
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
    """Calcula Root Mean Square Error entre duas listas de pontos."""
    n = min(len(pts_a), len(pts_b))
    if n < 2:
        return float("inf")

    distances = np.array([haversine_distance(pts_a[i], pts_b[i]) for i in range(n)])
    mean_squared = np.mean(distances**2)
    rmse = np.sqrt(mean_squared)
    return float(rmse)


# --- CLASSE PRINCIPAL ---


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    """
    Online matcher using Fixed Sliding Window (FSW) with Look-Ahead Stitching.
    Implementa Dual Stitching: Topológico (Edges) e Geométrico (Resampling + RMSE).
    """

    _base_matcher_name: Final[str] = "FSW"

    def __init__(
        self,
        matcher: BaseMatcher,
        *,
        window_size: int = 100,
        overlap_size: int = 20,
        lookahead: int = 1,
    ) -> None:
        super().__init__()
        if window_size <= 0 or overlap_size >= window_size:
            raise ValueError("Invalid window/overlap configuration.")
        
        if lookahead < 0:
            raise ValueError("lookahead must be >= 0.")

        self._offline_matcher = matcher
        self._window_size = window_size
        self._overlap_size = overlap_size
        self._lookahead = lookahead

        # Buffer de entrada (Raw Points) para formar janelas com overlap
        self._raw_buffer: list[GPSPoint] = []

        # Buffer de saídas (Resultados do Match) para decisão Look-Ahead
        self._window_results_buffer: deque[MatchResult] = deque()

        # Resultado acumulado
        self._result = OnlineMatchResult(matcher_name=self.matcher_name)

        # Executor para rodar o matcher offline sem bloquear o loop
        self._executor: ThreadPoolExecutor | None = None

    @property
    @override
    def matcher_name(self) -> str:
        return f"{self._base_matcher_name}({self._offline_matcher.matcher_name})"

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
            # 1. Acumula pontos brutos e atualiza o 'sent' (rastreamento de entrada)
            self._raw_buffer.append(point)
            self._result._update_sent(point)  # Método interno para rastrear input

            # 2. Se encheu a janela, dispara o match offline
            if len(self._raw_buffer) >= self._window_size:
                await self._process_current_raw_batch()

            # 3. Se temos janelas suficientes no buffer de resultados, tentamos costurar (Stitch)
            while len(self._window_results_buffer) > self._lookahead:
                yield await self._stitch_and_emit()

        # 4. Flush: processa o que sobrou no raw buffer
        if len(self._raw_buffer) > 0:
            await self._process_current_raw_batch()

        # 5. Flush: emite o restante dos resultados pendentes
        while len(self._window_results_buffer) > 0:
            yield await self._emit_remaining()

    async def _process_current_raw_batch(self):
        """Envia o batch atual para o matcher offline e gerencia o overlap da próxima entrada."""
        if not self._executor:
            return

        # Snapshot para a thread
        batch_snapshot = list(self._raw_buffer)

        # Executa Match Offline (Bloqueante) em outra thread
        loop = asyncio.get_running_loop()
        match_result = await loop.run_in_executor(
            self._executor, self._offline_matcher.match, batch_snapshot
        )

        self._window_results_buffer.append(match_result)

        # Prepara o raw_buffer para a próxima: Mantém os últimos 'overlap_size' pontos
        # para garantir que a próxima janela tenha contexto físico com esta.
        if self._overlap_size > 0:
            self._raw_buffer = self._raw_buffer[-self._overlap_size :]
        else:
            self._raw_buffer = []

    async def _stitch_and_emit(self) -> OnlineMatchResult:
        """Tenta costurar a janela atual (0) com a futura (1) e emite o resultado."""

        current_res = self._window_results_buffer[0]
        future_res = self._window_results_buffer[1]  # Lookahead=1 simples

        # --- 1. Overlap Topológico (Edges) ---
        edges_cut_idx = self._resolve_edge_overlap(
            current_res.edge_ids, future_res.edge_ids
        )
        final_edges = current_res.edge_ids[:edges_cut_idx]

        # --- 2. Overlap Geométrico (Pontos) ---
        geom_cut_idx = self._resolve_geometry_overlap(
            current_res.matched_points, future_res.matched_points
        )
        final_points = current_res.matched_points[:geom_cut_idx]

        # --- 3. Atualiza Estado e Emite ---
        self._append_to_result(final_edges, final_points)

        # Remove a janela processada. A 'future' vira a nova 'current'.
        self._window_results_buffer.popleft()

        # Retorna uma cópia do estado atual (snapshot)
        return (
            self._result
        )  # Assumindo que o consumidor fará deepcopy se necessário, ou use copy.deepcopy(self._result)

    async def _emit_remaining(self) -> OnlineMatchResult:
        """Emite a janela restante sem costura (fim do stream)."""
        res = self._window_results_buffer.popleft()
        self._append_to_result(res.edge_ids, res.matched_points)
        return self._result

    def _append_to_result(self, new_edges: list, new_points: list[Coordinate]):
        """Adiciona dados ao resultado acumulado, evitando duplicatas simples nas bordas."""

        # Adiciona Edges (evitando duplicar o último se for igual ao primeiro do novo)
        if (
            self._result.edge_ids
            and new_edges
            and self._result.edge_ids[-1] == new_edges[0]
        ):
            self._result.edge_ids.extend(new_edges[1:])
        else:
            self._result.edge_ids.extend(new_edges)

        # Adiciona Pontos (Geometria)
        # Nota: Para geometria, geralmente apendamos tudo, ou verificamos dist < epsilon
        self._result.matched_points.extend(new_points)

    # --- LÓGICA DE STITCHING ---

    def _resolve_edge_overlap(self, edges_a: list, edges_b: list) -> int:
        """Encontra onde A termina para B começar (Baseado em IDs)."""
        if not edges_b:
            return len(edges_a)

        # Procura o maior sufixo de A que é prefixo de B
        # Otimização: busca apenas na segunda metade de A
        start_search = len(edges_a) // 2

        for i in range(len(edges_a) - 1, start_search - 1, -1):
            if edges_a[i] == edges_b[0]:
                # Candidato a overlap
                overlap_len = len(edges_a) - i
                # Verifica se o resto bate
                if edges_a[i:] == edges_b[:overlap_len]:
                    return i  # Corta A aqui

        return len(edges_a)  # Sem overlap, retorna tudo

    def _resolve_geometry_overlap(
        self, geom_a: list[Coordinate], geom_b: list[Coordinate]
    ) -> int:
        """Encontra ponto de corte geométrico usando Reamostragem + RMSE."""
        if not geom_b or not geom_a:
            return len(geom_a)

        # 1. Cria assinatura do INÍCIO de B (ex: primeiros 20m normalizados)
        sig_b = resample_polyline(
            geom_b, PASSO_AMOSTRAGEM_METROS, COMPRIMENTO_TESTE_METROS
        )
        if not sig_b:
            return len(geom_a)

        best_cut_idx = len(geom_a)
        min_rmse = float("inf")

        # 2. Busca essa assinatura no FINAL de A
        start_search = int(len(geom_a) * 0.6)

        for i in range(start_search, len(geom_a)):
            # Tenta pegar um segmento em A começando em i
            candidate_segment = geom_a[i:]

            # Reamostra candidato A
            sig_a = resample_polyline(
                candidate_segment, PASSO_AMOSTRAGEM_METROS, COMPRIMENTO_TESTE_METROS
            )

            # Compara RMSE
            rmse = calculate_rmse(sig_a, sig_b)

            if rmse < min_rmse:
                min_rmse = rmse
                best_cut_idx = i

        # 3. Validação
        if min_rmse < LIMIAR_RMSE_METROS:
            return best_cut_idx

        return len(geom_a)  # Falha na convergência, retorna tudo


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)