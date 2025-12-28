import math
from typing import Any, Literal

import networkx as nx
import osmnx as ox
import pandas as pd
import plotly.graph_objects as go

# --- Types ---
type Lat = float
type Lon = float
type Point = tuple[Lat, Lon]
type EdgeID = str
type TraceType = Literal["markers", "lines"]


# --- Helper Functions ---
def _distance(a: Point, b: Point) -> float:
    """Calculate Euclidean distance between two points."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _create_scatter_trace(
    df: pd.DataFrame,
    trace_type: str,
    mode: TraceType,
    color: str | None = None,
    size: int | None = None,
    width: int | None = None,
    visible: bool = True,
) -> go.Scattermap:
    """Create a Scattermap trace for points or lines."""
    marker_dict = dict(size=size) if size else None
    line_dict = dict(width=width) if width else None

    return go.Scattermap(
        lat=df["lat"],
        lon=df["lon"],
        mode=mode,
        marker=marker_dict,
        line=line_dict,
        name=f"{trace_type} - {'pontos' if mode == 'markers' else 'linha'}",
        legendgroup=trace_type,
        visible=visible,
    )


def _create_layout(
    title: str,
    map_center: dict[str, float],
    zoom: float,
    updatemenus: list[dict[str, Any]] | None = None,
    height: int = 700,
) -> go.Layout:
    """Create the layout for the figure."""
    return go.Layout(
        mapbox_style="open-street-map",
        mapbox_center=map_center,
        mapbox_zoom=zoom,
        margin=dict(l=0, r=0, t=80, b=0),
        height=height,
        title=title,
        updatemenus=updatemenus,
    )


def _create_buttons(
    original_label: str,
    calculated_label: str,
    show_original_line: bool,
    map_center: dict[str, float],
    zoom: float,
) -> list[dict[str, Any]]:
    """Create the update buttons for the map."""
    return [
        dict(
            type="buttons",
            direction="right",
            showactive=True,
            x=0.5,
            xanchor="center",
            y=1,
            yanchor="top",
            buttons=[
                dict(
                    label="Mostrar Nenhuma",
                    method="update",
                    args=[
                        {"visible": [False, False, False, False]},
                        {"mapbox.center": map_center, "mapbox.zoom": zoom},
                    ],
                ),
                dict(
                    label="Mostrar Ambas",
                    method="update",
                    args=[
                        {"visible": [True, show_original_line, True, True]},
                        {"mapbox.center": map_center, "mapbox.zoom": zoom},
                    ],
                ),
                dict(
                    label=f"Apenas {original_label}",
                    method="update",
                    args=[
                        {"visible": [True, show_original_line, False, False]},
                        {"mapbox.center": map_center, "mapbox.zoom": zoom},
                    ],
                ),
                dict(
                    label=f"Apenas {calculated_label}",
                    method="update",
                    args=[
                        {"visible": [False, False, True, True]},
                        {"mapbox.center": map_center, "mapbox.zoom": zoom},
                    ],
                ),
            ],
        )
    ]


def _map_osmid_to_edges(
    graph: nx.Graph | nx.MultiDiGraph,
) -> dict[str, list[tuple[int, int]]]:
    """Map OSM IDs to graph edges."""
    osmid_to_edge_map: dict[str, list[tuple[int, int]]] = {}
    for u, v, data in graph.edges(data=True):
        osmids = data.get("osmid")
        if osmids is None:
            continue

        if not isinstance(osmids, (list, tuple, set)):
            osmids = [osmids]

        for osm in osmids:
            osmid_str = str(osm)
            osmid_to_edge_map.setdefault(osmid_str, []).append((u, v))

        osmid_to_edge_map.setdefault(str(u), []).append((u, v))

    for node in graph.nodes():
        osmid_to_edge_map.setdefault(str(node), []).append((node, node))

    return osmid_to_edge_map


def _create_path_trace(
    osmid_list: list[EdgeID],
    osmid_to_edge_map: dict[str, list[tuple[int, int]]],
    pos: dict[int, tuple[float, float]],
    color: str,
    name: str,
    width: int = 4,
) -> go.Scatter:
    """Create a trace for a path of OSM IDs."""
    x_coords, y_coords = [], []
    hover_texts = []
    prev_coords = None
    missing = []

    for osmid in osmid_list:
        candidates = osmid_to_edge_map.get(str(osmid))
        if not candidates:
            missing.append(osmid)
            continue

        for u, v in candidates:
            ux, uy = pos[u]
            vx, vy = pos[v]

            segment = [(ux, uy), (vx, vy)]

            # if previous segment doesn't end at this start, try to reverse for continuity
            if prev_coords and prev_coords != segment[0]:
                if prev_coords == segment[1]:
                    segment.reverse()
                elif not _distance(prev_coords, segment[0]) < 0.00005:  # ~5m tolerance
                    # if still not connected, insert a gap (None)
                    x_coords.append(None)
                    y_coords.append(None)
                    hover_texts.append(None)

            # add to path
            for x, y in segment:
                x_coords.append(x)
                y_coords.append(y)
                hover_texts.append(f"osmid: {osmid}")

            prev_coords = segment[-1]

    if missing:
        print(
            f"⚠️  Warning: {len(missing)} OSMIDs not found in graph: {missing[:5]}{'...' if len(missing) > 5 else ''}"
        )

    return go.Scatter(
        x=x_coords,
        y=y_coords,
        mode="lines",
        line=dict(color=color, width=width),
        name=name,
        visible=True,
        hoverinfo="text",
        text=hover_texts,
    )


# --- Main Functions ---
def plot_trajectories(
    original: list[Point],
    calculated: list[Point],
    title: str = "Tracks",
    original_label: str = "Original",
    calculated_label: str = "Calculated",
    show_original_line: bool = True,
    show_buttons: bool = True,
    center_lat: float | None = None,
    center_lon: float | None = None,
    zoom: float = 14,
) -> None:
    """Plot original and calculated trajectories on a map."""
    flat_original = [
        (lat, lon, original_label, i) for i, (lat, lon) in enumerate(original)
    ]
    flat_calculated = [
        (lat, lon, calculated_label, i) for i, (lat, lon) in enumerate(calculated)
    ]

    df = pd.DataFrame(
        flat_original + flat_calculated,
        columns=["lat", "lon", "type", "row_num"],  # type: ignore
    )

    fig = go.Figure()

    for type_ in [original_label, calculated_label]:
        subset = df[df["type"] == type_].sort_values(by=["row_num"])  # type: ignore

        # Points
        fig.add_trace(
            _create_scatter_trace(subset, type_, "markers", size=10, visible=True)
        )

        # Lines
        show_line = True
        if type_ == original_label and not show_original_line:
            show_line = False

        fig.add_trace(
            _create_scatter_trace(subset, type_, "lines", width=2, visible=show_line)
        )

    # Define map center
    if center_lat is None or center_lon is None:
        map_center = dict(lat=float(df["lat"].mean()), lon=float(df["lon"].mean()))
    else:
        map_center = dict(lat=center_lat, lon=center_lon)

    updatemenus = None
    if show_buttons:
        updatemenus = _create_buttons(
            original_label, calculated_label, show_original_line, map_center, zoom
        )

    fig.update_layout(_create_layout(title, map_center, zoom, updatemenus))
    fig.update_layout(
        mapbox_style="open-street-map",
        mapbox_center=map_center,
        mapbox_zoom=zoom,
    )

    fig.show()


def plot_map_matching_from_osmid(
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
    if isinstance(graph, str):
        print(f"Loading graph for place: {graph}...")
        graph = ox.graph_from_place(graph, network_type="drive")
        print("Graph loaded.")

    # --- Node positions ---
    pos = {node: (data["x"], data["y"]) for node, data in graph.nodes(data=True)}

    # --- OSMID → Edges map ---
    osmid_to_edge_map = _map_osmid_to_edges(graph)

    # --- Figure data ---
    traces = []

    # 0. Background
    x_bg, y_bg, hover_bg = [], [], []
    for u, v, data in graph.edges(data=True):
        x_bg.extend([pos[u][0], pos[v][0], None])
        y_bg.extend([pos[u][1], pos[v][1], None])
        osmid = data.get("osmid", "")
        hover_bg.extend([f"osmid: {osmid}", f"osmid: {osmid}", None])

    traces.append(
        go.Scatter(
            x=x_bg,
            y=y_bg,
            mode="lines",
            line=dict(color="lightgray", width=1),
            name="Street Network",
            hoverinfo="text",
            text=hover_bg,
            visible=True,
        )
    )

    # 1. Ground Truth
    if ground_truth_osmid_path:
        traces.append(
            _create_path_trace(
                ground_truth_osmid_path,
                osmid_to_edge_map,
                pos,
                "green",
                "Ground Truth",
            )
        )

    # 2. Map Matched
    if map_matched_osmid_path:
        traces.append(
            _create_path_trace(
                map_matched_osmid_path,
                osmid_to_edge_map,
                pos,
                "blue",
                "Map Matched",
            )
        )

    node_markers = go.Scatter(
        x=[x for x, y in pos.values()],
        y=[y for x, y in pos.values()],
        mode="markers",
        marker=dict(size=2, color="black"),
        name="Nodes",
        hoverinfo="text",
        text=[f"NodeID: {node}" for node in graph.nodes()],
        visible=False,
    )

    # --- Layout and buttons ---
    show_nodes = dict(
        label="Show Nodes",
        method="update",
        args=[{"visible": [True, True, True, True]}],
        args2=[{"visible": [True, True, True, False]}],
    )

    only_gt = dict(
        label="Only Ground Truth",
        method="update",
        args=[{"visible": [True, True, False]}],
        args2=[{"visible": [True, True, True]}],
    )

    only_mm = dict(
        label="Only Map Matched",
        method="update",
        args=[{"visible": [True, False, True]}],
        args2=[{"visible": [True, True, True]}],
    )

    both = dict(
        label="Both",
        method="update",
        args=[{"visible": [True, True, True]}],
        args2=[{"visible": [True, True, True]}],
    )

    neither = dict(
        label="Neither",
        method="update",
        args=[{"visible": [True, False, False]}],
        args2=[{"visible": [True, True, True]}],
    )

    fig = go.Figure(data=traces)
    fig.add_trace(node_markers)
    fig.update_layout(
        title="Map Matching Visualization",
        showlegend=True,
        legend=dict(x=0.01, y=0.99),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(
            showgrid=False,
            zeroline=False,
            showticklabels=False,
            scaleanchor="x",
            scaleratio=1,
        ),
        plot_bgcolor="white",
        margin=dict(l=10, r=10, t=40, b=10),
        updatemenus=[
            dict(
                type="buttons",
                direction="left",
                x=0.5,
                y=1.1,
                xanchor="center",
                yanchor="top",
                buttons=[both, only_gt, only_mm, neither, show_nodes],
            )
        ],
    )

    gt_edges = set(ground_truth_osmid_path)
    mm_edges = set(map_matched_osmid_path)

    matched = gt_edges & mm_edges
    added = mm_edges - gt_edges
    missing = gt_edges - mm_edges
    difference = gt_edges ^ mm_edges

    fig.update_layout(
        margin=dict(l=10, r=10, t=40, b=60),  # more space below
        annotations=[
            dict(
                text=(
                    f"Matched: {len(matched)} | "
                    f"Added: {len(added)} | "
                    f"Missing: {len(missing)} | "
                    f"Difference: {len(difference)}"
                ),
                showarrow=False,
                xref="paper",
                yref="paper",
                x=0.5,
                y=-0.1,
                xanchor="center",
                font=dict(size=12),
            )
        ],
    )

    fig.update_layout(height=700)

    fig.update_layout(
        dragmode="pan",
        hovermode="closest",
    )

    fig.show(config=dict(scrollZoom=True, displayModeBar=True))
