import folium
import networkx as nx
from mmlib.plot._plot_on_map import _plot_on_map, Point
from mmlib.plot._plot_on_graph import _plot_on_graph, EdgeID
from mmlib.plot._plot_on_folium import _plot_on_folium


def plot_on_map(
    original: list[Point],
    calculated: list[Point],
    title: str = "Tracks",
    original_label: str = "Original",
    calculated_label: str = "Calculated",
    show_original_line: bool = True,
    show_buttons: bool = True,
    center_lat: float | None = None,
    center_lon: float | None = None,
    zoom: int = 14,
) -> None:
    """
    Plot original and calculated trajectories on a map using Plotly Express.

    Args:
        original: List of (lat, lon) tuples for original trajectory
        calculated: List of (lat, lon) tuples for calculated trajectory
        title: Map title
        original_label: Label for original trajectory
        calculated_label: Label for calculated trajectory
        show_original_line: Whether to show original trajectory line
        show_buttons: Whether to show toggle buttons
        center_lat: Map center latitude (auto-calculated if None)
        center_lon: Map center longitude (auto-calculated if None)
        zoom: Map zoom level
    """
    return _plot_on_map(
        original=original,
        calculated=calculated,
        title=title,
        original_label=original_label,
        calculated_label=calculated_label,
        show_original_line=show_original_line,
        show_buttons=show_buttons,
        center_lat=center_lat,
        center_lon=center_lon,
        zoom=zoom,
    )


def plot_on_graph(
    graph: nx.Graph | nx.MultiDiGraph | str,
    ground_truth_osmid_path: list[EdgeID],
    map_matched_osmid_path: list[EdgeID],
) -> None:
    """
    Plot ground truth and map matched paths on an OSMnx graph from lists of osmids.

    Args:
        graph: NetworkX graph or place name (str) to load from OSMnx.
        ground_truth_osmid_path: List of OSM IDs for ground truth path.
        map_matched_osmid_path: List of OSM IDs for matched path.
    """
    return _plot_on_graph(
        graph=graph,
        ground_truth_osmid_path=ground_truth_osmid_path,
        map_matched_osmid_path=map_matched_osmid_path,
    )


def plot_on_folium(
    graph: nx.MultiDiGraph,
    ground_truth_osmid_path: list[EdgeID],
    map_matched_osmid_path: list[EdgeID],
    zoom_start: int = 15,
) -> folium.Map:
    """
    Folium version of map-matching visualization.

    Shows:
      - Street network (gray)
      - Ground truth path (green)
      - Map matched path (blue)

    Includes tooltips, layer toggles and summary statistics.

    Args:
        graph: NetworkX MultiDiGraph from OSMnx
        ground_truth_osmid_path: List of OSM IDs for ground truth path
        map_matched_osmid_path: List of OSM IDs for matched path
        zoom_start: Initial zoom level for the map

    Returns:
        Folium Map object
    """
    return _plot_on_folium(
        graph=graph,
        ground_truth_osmid_path=ground_truth_osmid_path,
        map_matched_osmid_path=map_matched_osmid_path,
        zoom_start=zoom_start,
    )
