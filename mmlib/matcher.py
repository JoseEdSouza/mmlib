from abc import ABC, abstractmethod

from dataclasses import dataclass

from mmlib.gpx import GPSPoint


@dataclass
class Coordinate:
    """Represents a geographical coordinate."""

    latitude: float
    longitude: float

    def to_tuple(self) -> tuple[float, float]:
        """Convert the coordinate to a tuple representation.

        Returns:
        tuple[float, float]: The latitude and longitude as a tuple.
        """
        return (self.latitude, self.longitude)


@dataclass
class MatchResult:
    """Represents the result of a map matching operation."""

    _matcher_name: str
    _measurement_points: list[Coordinate]
    matched_points: list[Coordinate]
    edge_ids: list[str]

    @classmethod
    def __from_internal(cls, matcher_name: str, measurement_points: list[Coordinate], matched_points: list[Coordinate], edge_ids: list[str]) -> "MatchResult":
        return cls(
            _matcher_name=matcher_name,
            _measurement_points=measurement_points,
            matched_points=matched_points,
            edge_ids=edge_ids,
        )

    def plot(self) -> None:
        """Plot the matched points and edges using folium."""
        from .mmplot import plot_trajectories

        plot_trajectories(
            original=[pt.to_tuple() for pt in self._measurement_points],
            calculated=[pt.to_tuple() for pt in self.matched_points],
            title=f"Map Matching - {self._matcher_name}",
            original_label="Original GPS",
            calculated_label=f"{self._matcher_name} Match",
            show_original_line=False,
            zoom=16,
        )


class BaseMatcher(ABC):
    """Matcher is a base class for map matching implementations."""

    @abstractmethod
    def map_match(self, points: list[GPSPoint]) -> MatchResult:
        """Map match the provided GPX points.

        Args:
            gpx_points (str): The GPX points to map match.

        Returns:
            MatchResult: The result of the map matching process.
        """
        ...


def graphhopper_matcher(base_url: str, gps_accuracy: int | None = None) -> BaseMatcher:
    """Create a GraphHopper matcher instance."""
    from mmlib.graphhopper.matcher import Matcher

    return Matcher(base_url=base_url, gps_accuracy=gps_accuracy)
