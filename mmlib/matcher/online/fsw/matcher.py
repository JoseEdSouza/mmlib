import asyncio
import copy
import math
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import AsyncIterable, AsyncIterator, Final, Optional, override

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result.offline import MatchResult
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory


@dataclass(frozen=True)
class WindowResult:
    """Resultado da caixa-preta para uma janela."""
    edges: list
    points: list  # polyline: list[[lat, lon] or GPSPoint-like]


@dataclass(frozen=True)
class EdgeOverlap:
    valid: bool
    cut_idx_in_A: int          # A.edges[:cut_idx_in_A] é o que commitamos
    overlap_len: int = 0


@dataclass(frozen=True)
class GeoOverlap:
    valid: bool
    cut_dist_in_A: float       # metros desde o início de A.points
    start_dist_in_B: float     # metros desde o início de B.points
    rmse: float = math.inf
    overlap_dist: float = 0.0


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    """
    Online wrapper de um matcher offline (caixa-preta), usando:
      - FSW: janelas fixas com overlap na requisição
      - Look-ahead: espera N janelas futuras para decidir costura
      - Dual Overlap: edge_ids e geometria têm costura independente
    """

    _base_matcher_name: Final[str] = "FSW"

    def __init__(
        self,
        matcher: BaseMatcher,
        *,
        window_size: int = 100,          # TAM_JANELA (em número de pontos do stream)
        request_overlap: int = 20,       # TAM_OVERLAP_REQ (em número de pontos do stream)
        lookahead: int = 2,              # N_LOOKAHEAD
        min_points_to_match: int = 10,   # evita chamar caixa-preta com janela pequena
        max_workers: int = 1,
        # Geometria
        sample_step_m: float = 5.0,
        rmse_max_m: float = 15.0,
        geo_overlap_min_m: float = 40.0,
        geo_overlap_max_m: float = 300.0,
        rmse_shift_k: int = 3,
        # Edges
        min_overlap_edges: int = 3,
    ) -> None:
        super().__init__()
        if window_size <= 0:
            raise ValueError("window_size must be > 0")
        if request_overlap < 0 or request_overlap >= window_size:
            raise ValueError("request_overlap must be in [0, window_size-1]")
        if lookahead < 0:
            raise ValueError("lookahead must be >= 0")

        self._matcher = matcher
        self._window_size = window_size
        self._request_overlap = request_overlap
        self._stride = window_size - request_overlap
        self._lookahead = lookahead
        self._min_points_to_match = min_points_to_match

        self._sample_step_m = sample_step_m
        self._rmse_max_m = rmse_max_m
        self._geo_overlap_min_m = geo_overlap_min_m
        self._geo_overlap_max_m = geo_overlap_max_m
        self._rmse_shift_k = rmse_shift_k
        self._min_overlap_edges = min_overlap_edges

        self._executor = ThreadPoolExecutor(max_workers=max_workers)

        # buffers
        self._raw_buffer: list[GPSPoint] = []      # pontos brutos acumulados do stream
        self._results_buffer: deque[WindowResult] = deque()  # janelas matchadas pendentes

        # saída consolidada
        self._out_edges: list[str] = []
        self._out_points: list[Coordinate] = []

        # Result stream
        self._result = OnlineMatchResult(matcher_name=self.matcher_name)

    @property
    def matcher_name(self) -> str:
        return f"{self._base_matcher_name}({self._matcher.matcher_name})"

    @override
    async def start(self) -> None:
        if not self._started:
            self._started = True
            # recria executor (se stop() foi chamado)
            if getattr(self, "_executor", None) is None:
                self._executor = ThreadPoolExecutor(max_workers=1)

    @override
    async def stop(self) -> None:
        if self._started:
            self._executor.shutdown(wait=True)
            self._started = False

    # ----------------------------
    # API principal
    # ----------------------------
    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        await self.start()

        async for p in points:
            self._raw_buffer.append(p)

            # tenta produzir novas janelas matchadas sempre que tiver window_size
            await self._fill_results_buffer_if_possible()

            # tenta consolidar enquanto tiver lookahead suficiente
            while self._can_decide_stitch():
                self._stitch_one_step()

                # atualiza objeto OnlineMatchResult (stream)
                self._result.edge_ids = list(self._out_edges)
                # mantém a convenção antiga (matched_points), mas aqui vira polyline consolidada
                self._result.matched_points = list(self._out_points)

                yield copy.deepcopy(self._result)

        # flush final: processa o que sobrou
        await self._flush_remaining()

        self._result.edge_ids = list(self._out_edges)
        self._result.matched_points = list(self._out_points)
        yield copy.deepcopy(self._result)

    # ----------------------------
    # Janela -> caixa-preta
    # ----------------------------
    async def _fill_results_buffer_if_possible(self) -> None:
        """
        Enquanto tiver dados suficientes em _raw_buffer para formar uma janela,
        roda o matcher offline e enfileira WindowResult.
        """
        while len(self._raw_buffer) >= self._window_size:
            window_points = self._raw_buffer[: self._window_size]

            if len(window_points) < self._min_points_to_match:
                return

            loop = asyncio.get_running_loop()
            match_result: MatchResult = await loop.run_in_executor(
                self._executor, self._matcher.match, list(window_points)
            )

            wr = WindowResult(
                edges=list(match_result.edge_ids),
                points=list(match_result.matched_points),
            )
            self._results_buffer.append(wr)

            # avança no stream bruto pelo stride (mantém overlap no bruto)
            del self._raw_buffer[: self._stride]

    def _can_decide_stitch(self) -> bool:
        # Precisamos de pelo menos 2 janelas para costurar
        if len(self._results_buffer) <= 1:
            return False
        # Look-ahead N significa: decidir com A e até N futuras disponíveis (se possível)
        # Aqui basta ter 2; o algoritmo tenta até min(lookahead, n-1)
        return True

    async def _flush_remaining(self) -> None:
        """
        No final do stream, pode sobrar:
        - dados brutos insuficientes pra formar janela cheia
        - resultados em buffer sem futuras
        Estratégia: roda uma última janela com o que tiver (se passar do mínimo)
        e depois “anexa sem costura” tudo o que sobrar.
        """
        # tenta rodar uma última janela parcial
        if len(self._raw_buffer) >= self._min_points_to_match:
            loop = asyncio.get_running_loop()
            match_result: MatchResult = await loop.run_in_executor(
                self._executor, self._matcher.match, list(self._raw_buffer)
            )
            self._results_buffer.append(
                WindowResult(edges=list(match_result.edge_ids), points=list(match_result.matched_points))
            )
            self._raw_buffer.clear()

        # agora consolida tudo que sobrar do buffer, preferindo costuras quando possível
        while len(self._results_buffer) >= 2:
            self._stitch_one_step()

        # anexa a última janela restante (se existir)
        if self._results_buffer:
            last = self._results_buffer.popleft()
            self._append_whole_window(last)

    # ----------------------------
    # Dual Overlap stitching
    # ----------------------------
    def _stitch_one_step(self) -> None:
        """
        Decide costura da janela alvo (A = buffer[0]) com alguma futura (k=1..lookahead).
        Se achar, comita prefixos (edges e geom com cortes independentes) e avança buffer.
        Se não achar, faz fallback conservador.
        """
        A = self._results_buffer[0]

        best_k: Optional[int] = None
        best_e: Optional[EdgeOverlap] = None
        best_g: Optional[GeoOverlap] = None

        max_k = min(self._lookahead, len(self._results_buffer) - 1)
        # first-fit robusto (preferir k=1)
        for k in range(1, max_k + 1):
            B = self._results_buffer[k]

            e = self._overlap_edges_suffix_prefix(A.edges, B.edges)
            if not e.valid:
                continue

            g = self._overlap_geom_resampling(A.points, B.points)
            if not g.valid:
                continue

            best_k, best_e, best_g = k, e, g
            break

        if best_k is not None and best_e and best_g:
            # 1) commit edges (topologia)
            self._out_edges.extend(A.edges[: best_e.cut_idx_in_A])

            # 2) commit points (geometria por distância)
            prefixA = self._extract_prefix_by_distance(A.points, best_g.cut_dist_in_A)
            self._out_points = self._concat_polyline(self._out_points, prefixA)

            # 3) cortar prefixo redundante de B.points (evitar duplicar)
            B = self._results_buffer[best_k]
            B_points_adj = self._extract_suffix_by_distance(
                B.points, self._polyline_length(B.points) - best_g.start_dist_in_B
            )
            # atualiza o objeto B no buffer
            self._results_buffer[best_k] = WindowResult(edges=B.edges, points=B_points_adj)

            # 4) avançar: remove A e janelas intermediárias (mantém sua heurística)
            for _ in range(best_k):
                self._results_buffer.popleft()
        else:
            # fallback conservador: commit só uma fração segura do começo
            self._fallback_commit_min(A)
            self._results_buffer.popleft()

    def _append_whole_window(self, w: WindowResult) -> None:
        # edges: adiciona tudo, removendo duplicata simples de fronteira
        self._out_edges = self._concat_edges_no_dup(self._out_edges, w.edges)
        # pontos: concatena sem duplicar o ponto inicial se colado
        self._out_points = self._concat_polyline(self._out_points, w.points)

    def _fallback_commit_min(self, A: WindowResult) -> None:
        """
        Fallback seguro quando não há costura.
        Evita 'commit tudo' (que pode cristalizar erro).
        Estratégia simples: commit 30% inicial de edges e 30% inicial da geometria (por distância).
        """
        if not A.edges and not A.points:
            return

        # edges
        if A.edges:
            cut_e = max(1, int(len(A.edges) * 0.3))
            self._out_edges.extend(A.edges[:cut_e])

        # geom (por distância)
        if A.points:
            total = self._polyline_length(A.points)
            cut_d = max(self._geo_overlap_min_m, total * 0.3)
            prefix = self._extract_prefix_by_distance(A.points, cut_d)
            self._out_points = self._concat_polyline(self._out_points, prefix)

    # ----------------------------
    # Overlap edges (discreto)
    # ----------------------------
    def _overlap_edges_suffix_prefix(self, A: list, B: list) -> EdgeOverlap:
        if not A or not B:
            return EdgeOverlap(valid=False, cut_idx_in_A=len(A))

        Lmax = min(len(A), len(B))
        for L in range(Lmax, self._min_overlap_edges - 1, -1):
            if A[len(A) - L : len(A)] == B[0:L]:
                return EdgeOverlap(valid=True, cut_idx_in_A=len(A) - L, overlap_len=L)
        return EdgeOverlap(valid=False, cut_idx_in_A=len(A))

    # ----------------------------
    # Overlap geom (independente; por distância + reamostragem + RMSE)
    # ----------------------------
    def _overlap_geom_resampling(self, polyA: list, polyB: list) -> GeoOverlap:
        if len(polyA) < 2 or len(polyB) < 2:
            return GeoOverlap(valid=False, cut_dist_in_A=0.0, start_dist_in_B=0.0)

        A = self._resample_by_arc(polyA, self._sample_step_m)
        B = self._resample_by_arc(polyB, self._sample_step_m)

        best = GeoOverlap(valid=False, cut_dist_in_A=0.0, start_dist_in_B=0.0)

        # testa D do maior pro menor (preferir overlap longo)
        D = self._geo_overlap_max_m
        while D >= self._geo_overlap_min_m:
            n = int(D / self._sample_step_m)
            if n >= 5 and n <= len(A) and n <= len(B):
                sufA = A[len(A) - n :]
                preB = B[:n]
                rmse, shift = self._rmse_shift(sufA, preB, self._rmse_shift_k)

                if rmse < self._rmse_max_m:
                    cutA = max(0.0, self._polyline_length(polyA) - D)
                    startB = float(shift) * self._sample_step_m
                    best = GeoOverlap(
                        valid=True,
                        cut_dist_in_A=cutA,
                        start_dist_in_B=startB,
                        rmse=rmse,
                        overlap_dist=D,
                    )
                    return best  # first-fit por D decrescente
            D -= 2.0 * self._sample_step_m

        return best

    # ----------------------------
    # Geometria utilitária (sem 1:1)
    # ----------------------------
    def _rmse_shift(self, X: list, Y: list, K: int) -> tuple[float, int]:
        best_rmse = math.inf
        best_shift = 0
        for s in range(0, K + 1):
            n = min(len(X), len(Y) - s)
            if n < 5:
                continue
            acc = 0.0
            for i in range(n):
                d = self._dist_m(X[i], Y[i + s])
                acc += d * d
            rmse = math.sqrt(acc / n)
            if rmse < best_rmse:
                best_rmse = rmse
                best_shift = s
        return best_rmse, best_shift

    def _resample_by_arc(self, poly: list, step_m: float) -> list:
        if len(poly) < 2:
            return list(poly)

        out = [poly[0]]
        dist_acc = 0.0
        dist_target = 0.0

        for i in range(len(poly) - 1):
            p1 = poly[i]
            p2 = poly[i + 1]
            seg = self._dist_m(p1, p2)
            if seg <= 0:
                continue

            while (dist_target + step_m) <= (dist_acc + seg):
                dist_target += step_m
                t = (dist_target - dist_acc) / seg
                out.append(self._lerp(p1, p2, t))

            dist_acc += seg

        return out

    def _extract_prefix_by_distance(self, poly: list[Coordinate], D: float) -> list[Coordinate]:
        if len(poly) < 2 or D <= 0:
            return [poly[0]] if poly else []

        out = [poly[0]]
        dist = 0.0

        for i in range(len(poly) - 1):
            p1 = poly[i]
            p2 = poly[i + 1]
            seg = self._dist_m(p1, p2)
            if seg <= 0:
                continue

            if dist + seg <= D:
                out.append(p2)
                dist += seg
            else:
                falta = D - dist
                t = max(0.0, min(1.0, falta / seg))
                out.append(self._lerp(p1, p2, t))
                return out

        return out

    def _extract_suffix_by_distance(self, poly: list, D: float) -> list:
        # últimos D metros
        if len(poly) < 2:
            return list(poly)
        rev = list(reversed(poly))
        pref = self._extract_prefix_by_distance(rev, D)
        return list(reversed(pref))

    def _polyline_length(self, poly: list) -> float:
        if len(poly) < 2:
            return 0.0
        total = 0.0
        for i in range(len(poly) - 1):
            total += self._dist_m(poly[i], poly[i + 1])
        return total

    def _concat_polyline(self, base: list, seg: list, eps_m: float = 1.0) -> list:
        if not base:
            return list(seg)
        if not seg:
            return list(base)
        if self._dist_m(base[-1], seg[0]) < eps_m:
            return base + seg[1:]
        return base + seg

    def _concat_edges_no_dup(self, base: list, seg: list) -> list:
        if not base:
            return list(seg)
        if not seg:
            return list(base)
        # remove duplicata simples de fronteira
        if base[-1] == seg[0]:
            return base + seg[1:]
        return base + seg

    # ----------------------------
    # Distância/interpolação
    # ----------------------------
    def _dist_m(self, a, b) -> float:
        """
        Distância em metros. Se GPSPoint tiver lat/lon, use haversine.
        Aqui deixei genérico: espera a e b serem (lat,lon) ou GPSPoint com .lat/.lon.
        """
        lat1, lon1 = self._get_latlon(a)
        lat2, lon2 = self._get_latlon(b)

        # haversine
        R = 6371000.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)

        h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
        return 2 * R * math.asin(math.sqrt(h))

    def _lerp(self, a, b, t: float) -> Coordinate:
        lat1, lon1 = self._get_latlon(a)
        lat2, lon2 = self._get_latlon(b)
        lat = lat1 + (lat2 - lat1) * t
        lon = lon1 + (lon2 - lon1) * t
        # retorna no mesmo “formato” de entrada (tuple lat/lon).
        return Coordinate(lat=lat, lon=lon)

    def _get_latlon(self, p) -> Coordinate:
        # tenta atributos típicos
        if hasattr(p, "lat") and hasattr(p, "lon"):
            return Coordinate(lat=float(p.lat), lon=float(p.lon))
        if hasattr(p, "latitude") and hasattr(p, "longitude"):
            return Coordinate(lat=float(p.latitude), lon=float(p.longitude))  # type: ignore
        if isinstance(p, (tuple, list)) and len(p) >= 2:
            return Coordinate(lat=float(p[0]), lon=float(p[1]))
        raise TypeError("Point must be (lat,lon) or have lat/lon attributes")


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)