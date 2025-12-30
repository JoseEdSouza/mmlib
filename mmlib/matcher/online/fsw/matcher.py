import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
from typing import AsyncIterable, AsyncIterator, Final, List, override

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result.offline import MatchResult
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import GPSPoint
from mmlib.utils import factory


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    """Online matcher that applies a fixed sliding window strategy over a base matcher."""

    _base_matcher_name: Final[str] = "fixed_sliding_window"

    def __init__(
        self, matcher: BaseMatcher, *, window_size: int = 10, min_window_size: int = 5
    ) -> None:
        super().__init__()
        if window_size <= 0:
            raise ValueError("window_size must be a positive integer.")

        self._matcher = matcher
        self._window_size = window_size
        self._min_window_size = min_window_size

        # Estado acumulado
        self._result = OnlineMatchResult(matcher_name=self.matcher_name)
        self._window = deque[GPSPoint](maxlen=window_size)

        # Controle de execução
        self._executor = None

    @property
    def matcher_name(self) -> str:
        return f"{self._base_matcher_name}({self._matcher.matcher_name})"

    @override
    async def start(self) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1)

    @override
    async def stop(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        await self.start()

        async for point in points:
            self._window.append(point)

            if len(self._window) < self._min_window_size:
                continue

            window_snapshot = list(self._window)

            loop = asyncio.get_running_loop()
            match_result = await loop.run_in_executor(
                self._executor, self._matcher.match, window_snapshot
            )

            self._aggregate_result(match_result, window_snapshot)

            yield copy.deepcopy(self._result)

    def _aggregate_result(
        self, new_result: MatchResult, window_snapshot: List[GPSPoint]
    ) -> None:
        """
        Faz a 'costura' (stitching) dos novos resultados no histórico acumulado,
        evitando duplicatas nas emendas sem quebrar loops de trajeto.
        """

        # 1. Atualiza pontos de medição (Input)
        # Como é janela deslizante, adicionamos apenas o ÚLTIMO ponto recebido
        # se o histórico já existir. Se for o início, adiciona tudo.
        if not self._result.measurement_points:
            for p in window_snapshot:
                self._result._update_sent(p)
        else:
            # Adiciona apenas o ponto novo (o último da janela)
            self._result._update_sent(window_snapshot[-1])

        # 2. Atualiza Matched Points (Geometria) com Costura Inteligente
        self._result.matched_points = self._stitch_lists(
            self._result.matched_points, new_result.matched_points
        )

        # 3. Atualiza Edge IDs com Costura Inteligente
        self._result.edge_ids = self._stitch_lists(
            self._result.edge_ids, new_result.edge_ids
        )

    @staticmethod
    def _stitch_lists[T](accumulated: list[T], new_segment: list[T]) -> list[T]:
        """
        Junta duas listas sobrepostas verificando a redundância apenas no final
        da lista acumulada e no início do novo segmento.
        """
        if not accumulated:
            return new_segment

        if not new_segment:
            return accumulated

        # Heurística: Verifica se o início do novo segmento já está no final do acumulado.
        # Olha para os últimos N pontos para tentar achar o ponto de corte.
        # Isso evita duplicar o trecho de sobreposição da janela.

        # Tamanho da busca (otimização para não varrer a lista inteira de histórico)
        search_depth = min(len(accumulated), len(new_segment) + 5)
        overlap_index = -1

        # Tenta encontrar onde o primeiro ponto do new_segment aparece no final do accumulated
        first_new = new_segment[0]

        # Varre de trás para frente até search_depth
        for i in range(1, search_depth + 1):
            if accumulated[-i] == first_new:
                # Candidato a sobreposição encontrado.
                # Verifica se o resto bate (overlap validation)
                match_len = min(i, len(new_segment))
                # Compara o final de accumulated com o início de new_segment
                if accumulated[-i : -i + match_len] == new_segment[:match_len]:
                    overlap_index = i
                    break

        if overlap_index != -1:
            # Temos sobreposição de tamanho 'overlap_index'.
            # Retornamos acumulado + parte nova (pulando a sobreposição)
            # Ex: Acc=[A, B, C], New=[C, D, E] -> overlap=1 (C) -> Retorna [A, B, C, D, E]

            # Nota: Às vezes o match muda ligeiramente o passado.
            # Aqui assumimos que o acumulado é a verdade ("congelado").
            return accumulated + new_segment[overlap_index:]
        else:
            # Sem sobreposição clara (pode haver um salto ou correção de mapa).
            # Anexa tudo.
            return accumulated + new_segment


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)
