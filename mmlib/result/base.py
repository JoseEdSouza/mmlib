from abc import ABC
from dataclasses import dataclass, field
from typing import Any

import folium
import networkx as nx
import pandas as pd

from mmlib.types import Coordinate, GPSPoint


@dataclass
class BaseMatchResult(ABC):
    """Base class for map matching results."""

    matcher_name: str
    measurement_points: list[GPSPoint] = field(default_factory=list)
    matched_points: list[Coordinate] = field(default_factory=list)
    edge_ids: list[str] = field(default_factory=list)

    def to_dataframe(self) -> pd.DataFrame:
        """Export results to a Pandas DataFrame."""

        # Ensure lists are same length for dataframe
        length = max(
            len(self.measurement_points), len(self.matched_points), len(self.edge_ids)
        )

        data: list[dict[str, Any]] = []
        for i in range(length):
            row: dict[str, Any] = {"step": i}

            # Measurement
            if i < len(self.measurement_points):
                pt = self.measurement_points[i]
                row["lat"] = pt.coordinate.lat
                row["lon"] = pt.coordinate.lon
                row["time"] = pt.time
            else:
                row["lat"] = None
                row["lon"] = None
                row["time"] = None

            # Matched
            if i < len(self.matched_points):
                mpt = self.matched_points[i]
                row["matched_lat"] = mpt.lat
                row["matched_lon"] = mpt.lon
            else:
                row["matched_lat"] = None
                row["matched_lon"] = None

            # Edge
            if i < len(self.edge_ids):
                row["edge_id"] = self.edge_ids[i]
            else:
                row["edge_id"] = None

            data.append(row)

        return pd.DataFrame(data)

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
        from mmlib.mmplot.plot import plot_on_map

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
        from mmlib.mmplot.plot import plot_on_graph

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
        from mmlib.mmplot.plot import plot_on_folium

        return plot_on_folium(
            graph=graph,
            ground_truth_osmid_path=ground_truth_edge_ids or [],
            map_matched_osmid_path=self.edge_ids,
            zoom_start=zoom_start,
        )
