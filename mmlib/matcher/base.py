from abc import ABC, abstractmethod
from typing import AsyncIterable, AsyncIterator, Self

from mmlib.result import MatchResult, OnlineMatchResult
from mmlib.types.points import GPSPoint


class BaseMatcher(ABC):
    """BaseMatcher is a base class for map matching implementations."""

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

    @abstractmethod
    async def __aenter__(self) -> Self:
        """Initialize resources (ZMQ context and socket)."""
        ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Clean up resources."""
        ...


    @abstractmethod
    def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """Map match the provided points as they arrive (streaming)."""
        ...

    async def match_batch(self, points: list[GPSPoint]) -> OnlineMatchResult:
        """
        Helper method to match a list of points in batch using the streaming implementation.
        Useful for users who don't want to deal with async generators manually.
        """

        async def _gen():
            for p in points:
                yield p

        # We assume the subclass implements match_stream and follows the updated API pattern (Context Manager)
        # However, BaseOnlineMatcher doesn't enforce Context Manager on itself,
        # so we check if self is a Context Manager or just call match_stream.
        # But wait, match_stream requires 'async with self' usually.
        # Ideally we wrap it.

        last_result = None

        # Check if the matcher supports async context manager protocol
        if hasattr(self, "__aenter__") and hasattr(self, "__aexit__"):
            async with self:  # type: ignore
                async for result in self.match_stream(_gen()):
                    last_result = result
        else:
            # Fallback if not a context manager (unlikely with new design)
            async for result in self.match_stream(_gen()):
                last_result = result

        if last_result is None:
            # Return empty result if no points matched
            # Use the return annotation type if possible, or assume OnlineMatchResult
            return OnlineMatchResult(matcher_name="unknown")

        return last_result
