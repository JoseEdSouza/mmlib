from dataclasses import dataclass, field

import networkx as nx

from mmlib.types import Coordinate, GPSPoint


@dataclass
class MatchResult:
    """Represents the result of a map matching operation."""

    matcher_name: str
    measurement_points: list[GPSPoint] = field(default_factory=list)
    matched_points: list[Coordinate] = field(default_factory=list)
    edge_ids: list[str] = field(default_factory=list)

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
            original=[pt.coordinate.as_tuple for pt in self.measurement_points],
            calculated=[pt.as_tuple for pt in self.matched_points],
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
            ground_truth_edge_ids: Optional list of ground-truth-edge IDs.
        """
        from mmlib.mmplot.plot import plot_map_matching_from_osmid

        plot_map_matching_from_osmid(
            graph=graph,
            ground_truth_osmid_path=ground_truth_edge_ids or [],
            map_matched_osmid_path=self.edge_ids,
        )
