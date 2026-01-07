from collections import OrderedDict
from typing import cast

import folium
import networkx as nx
import osmnx as ox
import pandas as pd
import geopandas as gpd

from mmlib.result import calculate_match_metrics, MatchMetrics

type EdgeID = str


# --- Private Helper Functions ---
def _convert_graph_to_geodataframes(
    graph: nx.MultiDiGraph,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Convert NetworkX graph to GeoDataFrames in WGS84 projection."""
    nodes, edges = ox.graph_to_gdfs(graph)
    edges_wgs = edges.to_crs(epsg=4326)
    edges_wgs.union_all
    nodes_wgs = nodes.to_crs(epsg=4326)
    nodes_wgs["osmid"] = nodes_wgs.index.copy().astype(str)
    return nodes_wgs, edges_wgs


def _calculate_map_center(edges_wgs: gpd.GeoDataFrame) -> tuple[float, float]:
    """Calculate the center point of the map from edges."""
    center = edges_wgs.union_all().centroid
    return center.y, center.x


def _create_base_map(center: tuple[float, float], zoom_start: int = 15) -> folium.Map:
    """Create base Folium map with initial configuration."""
    return folium.Map(
        location=list(center), zoom_start=zoom_start, control_scale=True, tiles=None
    )


def _add_tile_layers(m: folium.Map) -> None:
    """Add various tile layer options to the map."""
    folium.TileLayer(
        "OpenStreetMap",
        name="OpenStreetMap Light (30% Opacity)",
        opacity=0.3,
        show=False,
    ).add_to(m)

    folium.TileLayer(
        tiles="CartoDB dark_matter",
        name="CartoDB Dark Matter",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        show=False,
    ).add_to(m)

    folium.TileLayer(
        "CartoDB positron",
        name="CartoDB Positron",
        opacity=0.60,
        attr="&copy; <a href='https://carto.com/attributions'>CARTO</a>",
        show=True,
    ).add_to(m)


def _add_street_network_layer(m: folium.Map, edges_wgs: pd.DataFrame) -> None:
    """Add street network as a base layer."""
    folium.GeoJson(
        edges_wgs,
        name="Street Network",
        style_function=lambda x: {"color": "lightblue", "weight": 2, "opacity": 0.5},
        tooltip=folium.GeoJsonTooltip(fields=["osmid"], aliases=["OSMID"]),
    ).add_to(m)


def _add_nodes_layer(m: folium.Map, nodes_wgs: pd.DataFrame) -> None:
    """Add nodes as a toggleable layer."""
    folium.GeoJson(
        nodes_wgs,
        name="Nodes",
        marker=folium.Circle(
            radius=2, color="gray", weight=0.5, fill=True, fill_opacity=0.2
        ),
        tooltip=folium.GeoJsonTooltip(fields=["osmid"], aliases=["Node ID"]),
    ).add_to(m)


def _filter_edges_by_osmid(
    edges_df: pd.DataFrame, osmid_list: list[str]
) -> pd.DataFrame:
    """
    Filters a GeoDataFrame of edges by a list of OSMIDs,
    handling cases where the 'osmid' column contains lists.
    """
    osmid_set = set(map(str, osmid_list))

    def has_osmid(val):
        if isinstance(val, (list, tuple)):
            return any(str(v) in osmid_set for v in val)
        return str(val) in osmid_set

    mask = edges_df["osmid"].apply(has_osmid)
    return cast(pd.DataFrame, edges_df[mask])


def _add_path_layer(
    m: folium.Map,
    edges_wgs: pd.DataFrame,
    osmid_path: list[EdgeID],
    name: str,
    color: str,
) -> None:
    """Add a path layer to the map with specified color."""
    if not osmid_path:
        return

    path_edges = _filter_edges_by_osmid(edges_wgs, osmid_path)
    folium.GeoJson(
        path_edges,
        name=name,
        style_function=lambda x: {"color": color, "weight": 4, "opacity": 0.6},
        tooltip=folium.GeoJsonTooltip(fields=["osmid"], aliases=["OSMID"]),
    ).add_to(m)


# Removed _calculate_statistics as it is replaced by calculate_match_metrics


def _build_statistics_html(metrics: MatchMetrics) -> str:
    """Build HTML table with statistics."""
    rows = [
        ("Matched", metrics.matched_count),
        ("Added", metrics.added_count),
        ("Missing", metrics.missing_count),
        ("Precision", f"{metrics.precision:.4f}"),
        ("Recall", f"{metrics.recall:.4f}"),
        ("F1 Score", f"{metrics.f1_score:.4f}"),
        ("Error Rate", f"{metrics.error_rate:.4f}"),
        ("Accuracy", f"{metrics.accuracy:.4f}"),
    ]
    if metrics.newson_krumm_error is not None:
        rows.append(("NK Error", f"{metrics.newson_krumm_error:.4f}"))

    stats_rows = "".join(
        f"""
        <tr>
            <td>{key}</td>
            <td style="text-align:right;">{value}</td>
        </tr>
        <tr><td colspan="2"><hr style="margin:2px 0; border:none; border-top:1px solid #222;"></td></tr>
        """
        for key, value in rows
    )

    return f"""
    <div style="background-color:white; padding:10px; border-radius:8px;
                box-shadow: 2px 2px 6px rgba(0,0,0,0.2); font-size:13px;
                position: fixed; right: 10px; bottom: 25px; z-index: 9999;
                width: 180px;">
        <b>Map Matching Summary</b>
        <table style="margin-top:5px; border-collapse:collapse; width:100%;">
            <tr>
                <th style="text-align:left; padding-right:10px;">Metric</th>
                <th style="text-align:right;">Value</th>
            </tr>
            {stats_rows}
        </table>
    </div>
    """


def _add_statistics_box(m: folium.Map, stats_html: str) -> None:
    """Add statistics box as a fixed legend to the map."""
    root = m.get_root()
    if isinstance(root, folium.Figure):
        root.html.add_child(folium.Element(stats_html))


def _add_layer_control(m: folium.Map) -> None:
    """Add layer control widget to the map."""
    folium.LayerControl(collapsed=False).add_to(m)


# --- Public API ---
def _plot_on_folium(
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

    gt_path = list(OrderedDict.fromkeys(ground_truth_osmid_path))
    mm_path = list(OrderedDict.fromkeys(map_matched_osmid_path))

    # Convert graph to GeoDataFrames
    nodes_wgs, edges_wgs = _convert_graph_to_geodataframes(graph)

    # Calculate map center and create base map
    center = _calculate_map_center(edges_wgs)
    m = _create_base_map(center, zoom_start)

    # Add tile layers
    _add_tile_layers(m)

    # Add street network and nodes
    _add_street_network_layer(m, edges_wgs)
    _add_nodes_layer(m, nodes_wgs)

    # Add path layers (map matched first, then ground truth on top)
    _add_path_layer(m, edges_wgs, mm_path, "Map Matched", "blue")
    _add_path_layer(m, edges_wgs, gt_path, "Ground Truth", "green")

    # Calculate and add statistics
    metrics = calculate_match_metrics(
        ground_truth_edges=gt_path, matched_edges=mm_path, graph=graph
    )
    stats_html = _build_statistics_html(metrics)
    _add_statistics_box(m, stats_html)

    # Add layer control
    _add_layer_control(m)

    return m
