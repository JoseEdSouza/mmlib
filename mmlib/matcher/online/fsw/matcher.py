import asyncio

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, Final, override

from mmlib.matcher.base import BaseMatcher, BaseOnlineMatcher
from mmlib.result.online import OnlineMatchResult
from mmlib.types.points import GPSPoint
from mmlib.utils import factory


class FixedSlidingWindowMatcher(BaseOnlineMatcher):
    _base_matcher_name: Final[str] = "FSW"

    def __init__(
        self,
        matcher: BaseMatcher,
        *,
        window_size: int = 100,
        lookahead: int = 10,
    ) -> None:
        super().__init__()
        if lookahead >= window_size:
            raise ValueError("lookahead must be < window_size")

        self._matcher = matcher
        self._window_size = window_size
        self._lookahead = lookahead
        self._commit_len = window_size - lookahead

        self._point_buffer: deque[GPSPoint] = deque(maxlen=window_size)
        self._committed_edges: list[str] = []

        self._executor: ThreadPoolExecutor | None = None

    @property
    @override
    def matcher_name(self) -> str:
        return f"{self._base_matcher_name}({self._matcher.matcher_name})"

    @override
    async def start(self) -> None:
        if not self._started:
            self._point_buffer.clear()
            self._committed_edges = []
            self._executor = ThreadPoolExecutor(max_workers=1)
            self._started = True

    @override
    async def stop(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None
        self._started = False

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        await self.start()
        loop = asyncio.get_running_loop()

        async for point in points:
            self._point_buffer.append(point)

            if len(self._point_buffer) < self._window_size:
                continue

            window_points = list(self._point_buffer)

            match_result = await loop.run_in_executor(
                self._executor,
                self._matcher.match,
                window_points,
            )

            edges = match_result.edge_ids
            if len(edges) < self._commit_len:
                continue

            # 🔒 Commit determinístico (prefixo)
            new_committed = edges[: self._commit_len]
            self._committed_edges.extend(new_committed)

            # desliza a janela
            for _ in range(self._commit_len):
                self._point_buffer.popleft()

            yield OnlineMatchResult(
                matcher_name=self.matcher_name,
                edge_ids=self._committed_edges.copy(),
            )

        # flush final
        if self._point_buffer:
            final_result = await loop.run_in_executor(
                self._executor,
                self._matcher.match,
                list(self._point_buffer),
            )
            self._committed_edges.extend(final_result.edge_ids)

            yield OnlineMatchResult(
                matcher_name=self.matcher_name,
                edge_ids=self._committed_edges.copy(),
            )


@factory(FixedSlidingWindowMatcher)
def fsw_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return FixedSlidingWindowMatcher(*args, **kwargs)
