from abc import ABC, abstractmethod
from dataclasses import dataclass

import networkx as nx

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

    matcher_name: str
    measurement_points: list[Coordinate]
    matched_points: list[Coordinate]
    edge_ids: list[str]

    def plot(
        self,
        title: str | None = None,
        original_label: str = "Original GPS",
        calculated_label: str | None = None,
        show_original_line: bool = False,
        zoom: float = 16,
    ) -> None:
        """
        Plot the matched points and edges.

        Args:
            title: Title of the plot. Defaults to "Map Matching - {matcher_name}".
            original_label: Label for original points. Defaults to "Original GPS".
            calculated_label: Label for matched points. Defaults to "{matcher_name} Match".
            show_original_line: Whether to show the line for original points. Defaults to False.
            zoom: Initial zoom level. Defaults to 16.
        """
        from mmlib.mmplot.plot import plot_trajectories

        if title is None:
            title = f"Map Matching - {self.matcher_name}"

        if calculated_label is None:
            calculated_label = f"{self.matcher_name} Match"

        plot_trajectories(
            original=[pt.to_tuple() for pt in self.measurement_points],
            calculated=[pt.to_tuple() for pt in self.matched_points],
            title=title,
            original_label=original_label,
            calculated_label=calculated_label,
            show_original_line=show_original_line,
            zoom=zoom,
        )

    def plot_on_graph(
        self,
        graph: nx.Graph | nx.MultiDiGraph | str,
        ground_truth_edge_ids: list[str] | None = None,
    ) -> None:
        """
        Plot the matched path on a graph.

        Args:
            graph: NetworkX graph representing the street network or a place name (str).
            ground_truth_edge_ids: Optional list of ground truth edge IDs.
        """
        from mmlib.mmplot.plot import plot_map_matching_from_osmid

        plot_map_matching_from_osmid(
            graph=graph,
            ground_truth_osmid_path=ground_truth_edge_ids or [],
            map_matched_osmid_path=self.edge_ids,
        )


class BaseMatcher(ABC):
    """Matcher is a base class for map matching implementations."""

    @abstractmethod
    def match(self, points: list[GPSPoint]) -> MatchResult:
        """Map match the provided GPX points.

        Args:
            points (list[GPSPoint]): The GPX points to map match.

        Returns:
            MatchResult: The result of the map matching process.
        """
        ...
