from abc import ABC
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import folium
import networkx as nx
import pandas as pd

from mmlib.types import Coordinate, GPSPoint
from mmlib.result.metrics import MatchMetrics, calculate_match_metrics


@dataclass
class BaseMatchResult(ABC):
    """Base class for map matching results."""

    matcher_name: str
    run_id: str = field(default_factory=lambda: uuid4().hex)
    measurement_points: list[GPSPoint] = field(default_factory=list)
    matched_points: list[Coordinate] = field(default_factory=list)
    edge_ids: list[str] = field(default_factory=list)

    def to_df(self, metrics: MatchMetrics | None = None) -> pd.DataFrame:
        """Export results to a Pandas DataFrame."""

        df = pd.DataFrame(
            {
                "run_id": [self.run_id],
                "matcher_name": [self.matcher_name],
                "measurement_points": [
                    [
                        (lat, lon, ts.isoformat())
                        for (lat, lon, ts) in self.measurement_points
                    ]
                ],
                "matched_points": [[p.as_tuple for p in self.matched_points]],
                "edge_ids": [[self.edge_ids]],
            }
        )

        if metrics:
            df.attrs.update(metrics.to_dict())

        return df

    def to_geojson(self) -> dict[str, Any]:
        """Export matched path as GeoJSON LineString."""
        coordinates = [[pt.lon, pt.lat] for pt in self.matched_points]
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates,
            },
            "properties": {
                "matcher": self.matcher_name,
                "point_count": len(coordinates),
            },
        }

    def plot(
        self,
        title: str | None = None,
        original_label: str = "Original GPS",
        calculated_label: str | None = None,
        show_original_line: bool = False,
        zoom: int = 16,
    ) -> None:
        """
        Plot the matched points and edges using mmlib.mmplot.
        """
        from mmlib.plot.plot import plot_on_map

        if title is None:
            title = f"Map Matching - {self.matcher_name}"

        if calculated_label is None:
            calculated_label = f"{self.matcher_name} Match"

        plot_on_map(
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
        """
        from mmlib.plot.plot import plot_on_graph

        plot_on_graph(
            graph=graph,
            ground_truth_osmid_path=ground_truth_edge_ids or [],
            map_matched_osmid_path=self.edge_ids,
        )

    def plot_on_folium(
        self,
        graph: nx.MultiDiGraph,
        ground_truth_edge_ids: list[str] | None = None,
        zoom_start: int = 15,
    ) -> folium.Map:
        """
        Plot the matched path on a folium map.
        """
        from mmlib.plot.plot import plot_on_folium

        return plot_on_folium(
            graph=graph,
            ground_truth_osmid_path=ground_truth_edge_ids or [],
            map_matched_osmid_path=self.edge_ids,
            zoom_start=zoom_start,
        )

    def calculate_metrics(
        self,
        ground_truth_edge_ids: list[str],
        run_id: str | None = None,
        graph: nx.Graph | nx.MultiDiGraph | None = None,
    ) -> MatchMetrics:
        """
        Calculate map matching evaluation metrics.
        """
        if run_id is None and hasattr(self, "run_id"):
            run_id = getattr(self, "run_id")

        return calculate_match_metrics(
            run_id=run_id,
            ground_truth_edges=ground_truth_edge_ids,
            matched_edges=self.edge_ids,
            graph=graph,
        )
