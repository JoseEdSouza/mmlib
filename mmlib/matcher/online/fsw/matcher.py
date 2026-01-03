import asyncio
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, override
from collections import deque, Counter

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result import OnlineMatchResult
from mmlib.result.offline import MatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    """FSW (Fixed Sliding Window) with fixed-lag lookahead online matcher."""

    @property
    @override
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
        self._all_points: list[GPSPoint] = []
        self._commited_geometry: list[Coordinate] = []
        self._pending_lookahead: list[list[str]] = []
        self._current_window_id: int = 0

    @override
    async def start(self) -> None:
        """Initialize buffers and state of the FSW."""
        if self._started:
            return
        self._executor: Executor = ThreadPoolExecutor(
            max_workers=self._lookahead_depth or 1
        )
        self._all_points.clear()
        self._point_buffer.clear()
        self._committed_path.clear()
        self._commited_geometry.clear()
        self._pending_lookahead.clear()
        self._current_window_id = 0
        self._started = True

    @override
    async def stop(self) -> None:
        """Clear buffers and state of the FSW."""
        if not self._started:
            return
        self._executor.shutdown(wait=True)
        self._point_buffer.clear()
        self._committed_path.clear()
        self._pending_lookahead.clear()

        self._started = False

    @staticmethod
    def _create_windows[T](
        sequence: list[T], window_size: int, lookahead_depth: int
    ) -> list[list[T]]:
        """Creates N+1 overlapping windows with lookahead."""
        points = list(sequence)
        windows: list[list[T]] = []

        buffer_len = len(points)
        for i in range(lookahead_depth + 1):
            start_idx = max(0, i * window_size)
            # Lookahead windows pegam pontos futuros
            end_idx = min(
                start_idx + window_size + (lookahead_depth - i) * (window_size // 2),
                buffer_len,
            )

            if start_idx < buffer_len:
                windows.append(points[start_idx:end_idx])

        return windows

    @staticmethod
    def _find_convergence_point[T](
        sequences: list[list[T]], convergence_depth: int
    ) -> int:
        """Finds the convergence point among multiple sequences."""
        if len(sequences) < 2 or not sequences[0]:
            return 0

        seq0 = sequences[0]
        max_overlap = min(
            len(seq0),
            len(sequences[1]) if len(sequences) > 1 else 0,
            convergence_depth,
        )

        for overlap_len in range(max_overlap, 0, -1):
            if (
                len(seq0) >= overlap_len
                and len(sequences[1]) >= overlap_len
                and seq0[-overlap_len:] == sequences[1][:overlap_len]
            ):
                return len(seq0) - overlap_len

        return max(0, len(seq0) - convergence_depth // 2)

    @staticmethod
    def _consensus_merge[T](sequences: list[list[T]]) -> list[T]:
        """Consensus merge of edge ID sequences."""
        if not sequences or not any(sequences):
            return []

        min_len = min((len(seq) for seq in sequences if seq), default=0)
        consensus: list[T] = []

        for pos in range(min_len):
            edges_at_pos = [seq[pos] for seq in sequences if pos < len(seq)]
            if edges_at_pos:
                most_common = Counter(edges_at_pos).most_common(1)[0][0]
                consensus.append(most_common)

        return consensus

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """Process gps points in sliding windows with fixed-lag lookahead."""
        if not self._started:
            raise RuntimeError("Matcher must be started first")

        points_processed = 0
        emit_counter = 0

        loop = asyncio.get_event_loop()

        async for point in points:
            self._point_buffer.append(point)
            self._all_points.append(point)
            points_processed += 1

            if len(self._point_buffer) < self._window_size:
                continue

            # Cria janelas e processa com matcher offline
            sequences = await self._run_windows(loop)

            self._current_window_id += 1

            if not sequences:
                continue

            edge_sequences = [seq.edge_ids for seq in sequences if seq.edge_ids]
            geometry_sequences = [
                seq.matched_points for seq in sequences if seq.matched_points
            ]

            new_committed_len = 0
            if edge_sequences:
                conv_edges = self._find_convergence_point(
                    edge_sequences, self._convergence_depth
                )
                primary_seq = edge_sequences[0]
                new_committed = primary_seq[: conv_edges + 1]
                new_committed_len = len(new_committed)
                self._committed_path.extend(new_committed)

                # ✅ FIX 1: CONSUMIR PONTOS PROCESSADOS
                # Remove pontos que já foram commitados
                points_to_remove = min(conv_edges + 1, len(self._point_buffer) // 2)
                for _ in range(points_to_remove):
                    if self._point_buffer:
                        self._point_buffer.popleft()

            if geometry_sequences:
                conv_geometry = self._find_convergence_point(
                    geometry_sequences, self._convergence_depth
                )
                primary_geom_seq = geometry_sequences[0]
                new_commited_geom = primary_geom_seq[: conv_geometry + 1]
                self._commited_geometry.extend(new_commited_geom)

            emit_counter += 1
            if emit_counter >= self._emit_every and new_committed_len > 0:
                edge_ids = self._dedup_list(self._committed_path)
                matched_points = self._commited_geometry.copy()
                yield OnlineMatchResult(
                    self.matcher_name,
                    edge_ids=edge_ids,
                    matched_points=matched_points or [],
                    measurement_points=self._all_points.copy(),
                )
                emit_counter = 0

        if len(self._point_buffer) != 0:

            results = await self._run_raw(loop)
            if results.edge_ids:
                self._committed_path.extend(results.edge_ids)

            if results.matched_points:
                self._commited_geometry.extend(results.matched_points)

        if self._committed_path:
            edge_ids = self._dedup_list(self._committed_path)
            matched_points = self._commited_geometry.copy()
            yield OnlineMatchResult(
                self.matcher_name,
                matched_points=matched_points or [],
                edge_ids=edge_ids,
                measurement_points=self._all_points.copy(),
                _finished=True,
            )

    async def _run_raw(self, loop: asyncio.AbstractEventLoop) -> MatchResult:
        result = await loop.run_in_executor(
            self._executor, self.offline_matcher.match, list(self._point_buffer)
        )
        return result

    async def _run_windows(self, loop: asyncio.AbstractEventLoop) -> list[MatchResult]:
        points_snapshot = list(self._point_buffer)
        windows = self._create_windows(
            points_snapshot, self._window_size, self._lookahead_depth
        )

        tasks = [
            loop.run_in_executor(self._executor, self.offline_matcher.match, win)
            for win in windows
        ]
        completed = await asyncio.gather(*tasks)

        return completed

    @staticmethod
    def _dedup_list[T](items: list[T]) -> list[T]:
        """Remove duplicatas consecutivas de uma lista."""
        if not items:
            return []

        deduped = [items[0]]
        for item in items[1:]:
            if item != deduped[-1]:
                deduped.append(item)
        return deduped


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)
