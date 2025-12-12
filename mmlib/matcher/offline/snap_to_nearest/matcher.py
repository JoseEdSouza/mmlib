
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import networkx as nx
import osmnx as ox
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point

from mmlib.matcher.base import BaseMatcher
from mmlib.result import MatchResult
from mmlib.types import Coordinate, GPSPoint
from mmlib.utils import factory


@dataclass
class _EdgeGeometry:
    """Helper structure holding edge geometry in the search CRS."""

    edge_id: tuple[object, object, int]
    geometry: LineString
    osmid: str


class SnapToNearestMatcher(BaseMatcher):
    """
    Offline matcher that snaps GPS points to the nearest road segment using an
    in-memory OSMnx graph. The graph is projected (by default) so distances and
    radius filtering are measured in meters.
    """

    _graph_latlon: nx.MultiDiGraph
    _graph_search: nx.MultiDiGraph
    _edge_geometries: list[_EdgeGeometry]
    _search_radius_m: float | None
    _to_search: Optional[Transformer]
    _to_latlon: Optional[Transformer]

    def __init__(
        self,
        graph: nx.MultiDiGraph,
        *,
        search_radius_m: float | None = 50.0,
        project_graph: bool = True,
    ) -> None:
        """
        Args:
            graph: OSMnx graph representing the street network (usually in WGS84).
            search_radius_m: Maximum allowed distance (meters) between a GPS point
                and its snapped location. If None, no distance filtering is applied.
            project_graph: When True (default), project the graph to a metric CRS for
                accurate distance calculations.
        """

        self._graph_latlon = graph
        self._graph_search = self._project_if_needed(graph, project_graph)
        self._search_radius_m = search_radius_m

        self._to_search, self._to_latlon = self._build_transformers(
            graph=self._graph_latlon, search_graph=self._graph_search
        )

        self._edge_geometries = list(self._prepare_edge_geometries(self._graph_search))

    def match(self, points: list[GPSPoint]) -> MatchResult:
        matched_points: list[Coordinate] = []
        edge_ids: list[str] = []

        for gps_point in points:
            snapped, edge_id = self._snap_point(gps_point)
            matched_points.append(snapped)
            edge_ids.append(edge_id)

        return MatchResult(
            matcher_name="SnapToNearest",
            measurement_points=list(points),
            matched_points=matched_points,
            edge_ids=edge_ids,
        )

    def _project_if_needed(
        self, graph: nx.MultiDiGraph, project_graph: bool
    ) -> nx.MultiDiGraph:
        if project_graph and not ox.projection.is_projected(graph):
            return ox.project_graph(graph)
        return graph

    def _build_transformers(
        self, graph: nx.MultiDiGraph, search_graph: nx.MultiDiGraph
    ) -> tuple[Optional[Transformer], Optional[Transformer]]:
        """
        Build forward/backward transformers between the input graph CRS and the search CRS.
        """
        source_crs_raw = graph.graph.get("crs", "EPSG:4326")
        search_crs_raw = search_graph.graph.get("crs", source_crs_raw)

        source_crs = CRS.from_user_input(source_crs_raw)
        search_crs = CRS.from_user_input(search_crs_raw)

        if source_crs == search_crs:
            return None, None

        to_search = Transformer.from_crs(source_crs, search_crs, always_xy=True)
        to_latlon = Transformer.from_crs(search_crs, source_crs, always_xy=True)
        return to_search, to_latlon

    def _prepare_edge_geometries(
        self, graph: nx.MultiDiGraph
    ) -> Iterable[_EdgeGeometry]:
        """
        Pre-compute edge geometries in the search CRS for fast snapping.
        """
        for u, v, key, data in graph.edges(keys=True, data=True):
            geom = data.get("geometry")
            if geom is None:
                # Build a straight line between the edge's nodes
                ux, uy = graph.nodes[u]["x"], graph.nodes[u]["y"]
                vx, vy = graph.nodes[v]["x"], graph.nodes[v]["y"]
                geom = LineString([(ux, uy), (vx, vy)])

            osmid = data.get("osmid")
            # Normalize osmid to a single string
            if isinstance(osmid, (list, tuple, set)):
                osmid = next(iter(osmid), None)  # type: ignore[arg-type]
            osmid_str = str(osmid) if osmid is not None else f"{u}-{v}"

            yield _EdgeGeometry(edge_id=(u, v, key), geometry=geom, osmid=osmid_str)

    def _snap_point(self, gps_point: GPSPoint) -> tuple[Coordinate, str]:
        pt_search = self._to_search_space(gps_point)

        closest_edge = None
        closest_distance = float("inf")

        # Brute force over edges is acceptable for small trajectories; can be swapped
        # for a spatial index (STRtree) if performance becomes an issue.
        for edge in self._edge_geometries:
            dist = pt_search.distance(edge.geometry)
            if dist < closest_distance:
                closest_distance = dist
                closest_edge = edge

        if closest_edge is None:
            # Should not happen with a valid graph
            return Coordinate(gps_point.lat, gps_point.lon), ""

        if self._search_radius_m is not None and closest_distance > self._search_radius_m:
            return Coordinate(gps_point.lat, gps_point.lon), ""

        snapped_point = closest_edge.geometry.interpolate(
            closest_edge.geometry.project(pt_search)
        )
        snapped_latlon = self._to_latlon_space(snapped_point)

        return snapped_latlon, closest_edge.osmid

    def _to_search_space(self, gps_point: GPSPoint) -> Point:
        lon, lat = gps_point.lon, gps_point.lat
        if self._to_search:
            lon, lat = self._to_search.transform(lon, lat)
        return Point(lon, lat)

    def _to_latlon_space(self, point: Point) -> Coordinate:
        lon, lat = point.x, point.y
        if self._to_latlon:
            lon, lat = self._to_latlon.transform(lon, lat)
        return Coordinate(lat=lat, lon=lon)


@factory(SnapToNearestMatcher)
def snap_to_nearest_matcher(*args, **kwargs) -> BaseMatcher:
    return SnapToNearestMatcher(*args, **kwargs)
