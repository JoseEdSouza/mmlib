from abc import ABC, abstractmethod
from typing import AsyncIterable

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
    """BaseMatcher is a base class for online map matching implementations."""

    @abstractmethod
    def match(self, points: AsyncIterable[GPSPoint]) -> OnlineMatchResult:
        """Map match the provided points as they arrive."""
