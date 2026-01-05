from abc import ABC, abstractmethod
from typing import AsyncIterable, AsyncIterator, Self

from mmlib.exceptions import MatcherInputError, MatcherRuntimeError
from mmlib.result import MatchResult, OnlineMatchResult
from mmlib.types.points import GPSPoint


class BaseMatcher(ABC):
    """BaseMatcher is a base class for map matching implementations."""

    @property
    @abstractmethod
    def matcher_name(self) -> str:
        """The name of the matcher."""
        ...

    @abstractmethod
    def match(self, points: list[GPSPoint]) -> MatchResult:
        """Map match the provided GPX points.

        Args:
            points (list[GPSPoint]): The GPX points to the map match.

        Returns:
            MatchResult: The result of the map matching process.
        """
        ...


class BaseOnlineMatcher(ABC):
    """BaseClass for online map matching implementations."""

    def __init__(self) -> None:
        self._started: bool = False

    @property
    @abstractmethod
    def matcher_name(self) -> str:
        """The name of the matcher."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Initialize resources."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Clean up resources."""
        ...

    async def __aenter__(self) -> Self:
        """Initialize resources"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool | None:
        """Clean up resources."""
        await self.stop()
        return None

    @abstractmethod
    def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """
        Match a stream of GPS points.
        Args:
            points (AsyncIterable[GPSPoint]): An async iterable of GPS points.

        Returns:
            AsyncIterator[OnlineMatchResult]: An async iterator of OnlineMatchResult.
        """
        ...

    async def match_batch(self, points: list[GPSPoint]) -> OnlineMatchResult:
        """
        Match a batch of GPS points.
        Args:
            points (list[GPSPoint]): A list of GPS points.
        Returns:
            OnlineMatchResult: The result of the online map matching process.
        """
        if not points:
            raise MatcherInputError("points must not be empty")

        async def _gen() -> AsyncIterator[GPSPoint]:
            for p in points:
                yield p

        last: OnlineMatchResult | None = None
        async with self:
            async for result in self.match_stream(_gen()):
                last = result

        # If points is not empty, "last is None" indicates a bug/broken contract
        if last is None:
            raise MatcherRuntimeError(
                "match_stream emitted no results for non-empty input"
            )

        return last
