from typing import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from collections import deque, Counter

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result import OnlineMatchResult
from mmlib.types.points import GPSPoint
from mmlib.utils import factory


@dataclass
class FSWConfig:
    # Emite resultado a cada N pontos
    pass


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    """FSW (Fixed Sliding Window) com Fixed-Lag Lookahead para BaseOnlineMatcher."""

    @property
    def matcher_name(self) -> str:
        return "FSW-FixedLag-Online"

    def __init__(
        self,
        offline_matcher: BaseMatcher,
        *,
        window_size: int = 50,
        lookahead_depth: int = 3,
        convergence_depth: int = 15,
        emit_every: int = 10,
    ):
        super().__init__()
        self.offline_matcher = offline_matcher

        # Estado interno (inicializado no start())
        if emit_every <= 0:
            raise ValueError("emit_every must be positive")

        if window_size <= 0:
            raise ValueError("window_size must be positive")

        if lookahead_depth < 0:
            raise ValueError("lookahead_depth must be non-negative")

        if convergence_depth <= 0:
            raise ValueError("convergence_depth must be positive")

        self._window_size = window_size
        self._lookahead_depth = lookahead_depth
        self._convergence_depth = convergence_depth
        self._emit_every = emit_every
        self._point_buffer: deque[GPSPoint] = deque(maxlen=self._window_size * 4)
        self._committed_path: list[str] = []
        self._pending_lookahead: list[list[str]] = []
        self._current_window_id: int = 0
        self._all_points_processed: list[GPSPoint] = []

    async def start(self) -> None:
        """Inicializa buffers e estado do FSW."""
        if self._started:
            return

        self._point_buffer.clear()
        self._committed_path.clear()
        self._pending_lookahead.clear()
        self._current_window_id = 0
        self._started = True

    async def stop(self) -> None:
        """Limpa todos os buffers e libera memória."""
        if not self._started:
            return

        self._point_buffer.clear()
        self._committed_path.clear()
        self._pending_lookahead.clear()

        self._started = False

    def _create_windows(self) -> list[list[GPSPoint]]:
        """Cria N+1 janelas deslizantes do buffer atual."""
        points = list(self._point_buffer)
        windows: list[list[GPSPoint]] = []

        buffer_len = len(points)
        for i in range(self._lookahead_depth + 1):
            start_idx = max(0, i * self._window_size)
            # Lookahead windows pegam pontos futuros
            end_idx = min(
                start_idx
                + self._window_size
                + (self._lookahead_depth - i) * (self._window_size // 2),
                buffer_len,
            )

            if start_idx < buffer_len:
                windows.append(points[start_idx:end_idx])

        return windows

    def _find_convergence_point(self, sequences: list[list[str]]) -> int:
        """Encontra convergence point entre sequências."""
        if len(sequences) < 2 or not sequences[0]:
            return 0

        seq0 = sequences[0]
        max_overlap = min(
            len(seq0),
            len(sequences[1]) if len(sequences) > 1 else 0,
            self._convergence_depth,
        )

        for overlap_len in range(max_overlap, 0, -1):
            if (
                len(seq0) >= overlap_len
                and len(sequences[1]) >= overlap_len
                and seq0[-overlap_len:] == sequences[1][:overlap_len]
            ):
                return len(seq0) - overlap_len

        return max(0, len(seq0) - self._convergence_depth // 2)

    def _consensus_merge(self, sequences: list[list[int]]) -> list[int]:
        """Merge consensual das sequências lookahead."""
        if not sequences or not any(sequences):
            return []

        min_len = min((len(seq) for seq in sequences if seq), default=0)
        consensus = []

        for pos in range(min_len):
            edges_at_pos = [seq[pos] for seq in sequences if pos < len(seq)]
            if edges_at_pos:
                most_common = Counter(edges_at_pos).most_common(1)[0][0]
                consensus.append(most_common)

        return consensus

    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """Processa stream de pontos com FSW + lookahead."""
        if not self._started:
            raise RuntimeError("Matcher must be started first")

        points_processed = 0
        emit_counter = 0

        async for point in points:
            self._point_buffer.append(point)
            points_processed += 1

            # Só processa quando tem buffer suficiente
            if len(self._point_buffer) >= self._window_size:
                # Cria janelas e processa com matcher offline
                windows = self._create_windows()
                sequences = [
                    self.offline_matcher.match(win).edge_ids
                    for win in windows
                    if len(win) >= 5
                ]  # Min pontos

                if sequences:
                    # Encontra convergence point e faz stitching
                    conv_point = self._find_convergence_point(sequences)
                    primary_seq = sequences[0]

                    # Commit novo segmento
                    new_committed = primary_seq[: conv_point + 1]
                    self._committed_path.extend(new_committed)

                    # Prepara lookahead para próxima iteração
                    self._pending_lookahead = [
                        seq[conv_point + 1 :] for seq in sequences[1:]
                    ]

                    emit_counter += 1
                    if emit_counter >= self._emit_every:
                        measurement_points = list(self._point_buffer)[
                            : len(self._committed_path)
                        ]
                        yield OnlineMatchResult(
                            self.matcher_name,
                            edge_ids=self._committed_path[-50:],
                            measurement_points=measurement_points,
                        )
                        emit_counter = 0
                self._current_window_id += 1

        # Final emit se sobrou buffer
        if self._committed_path:
            measurement_points = list(self._point_buffer)[: len(self._committed_path)]
            yield OnlineMatchResult(
                self.matcher_name,
                edge_ids=self._committed_path,
                measurement_points=measurement_points,
                _finished=True,
            )


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)
