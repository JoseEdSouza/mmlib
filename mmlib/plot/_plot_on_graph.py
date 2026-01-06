import math
import networkx as nx
import osmnx as ox
import plotly.graph_objects as go
from mmlib.result import calculate_match_metrics

# --- Types ---
type Lat = float
type Lon = float
type Point = tuple[Lat, Lon]
type EdgeID = str


# --- Private Helper Functions ---
def _distance(a: Point, b: Point) -> float:
    """Calculate Euclidean distance between two points."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


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


def _plot_on_graph(
    graph: nx.Graph | nx.MultiDiGraph | str,
    ground_truth_osmid_path: list[EdgeID],
    map_matched_osmid_path: list[EdgeID],
) -> None:
    """
    Plot ground truth and map matched paths on an OSMnx graph from lists of osmids.
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

    metrics = calculate_match_metrics(
        ground_truth_edges=ground_truth_osmid_path,
        matched_edges=map_matched_osmid_path,
        graph=graph,
    )

    nk_str = (
        f" | NK Error: {metrics.newson_krumm_error:.4f}"
        if metrics.newson_krumm_error is not None
        else ""
    )

    fig.update_layout(
        margin=dict(l=10, r=10, t=40, b=80),  # more space below
        annotations=[
            dict(
                text=(
                    f"Matched: {metrics.matched_count} | "
                    f"Added: {metrics.added_count} | "
                    f"Missing: {metrics.missing_count}<br>"
                    f"F1: {metrics.f1_score:.4f} | "
                    f"Acc: {metrics.accuracy:.4f} | "
                    f"Prec: {metrics.precision:.4f} | "
                    f"Rec: {metrics.recall:.4f}"
                    f"{nk_str}"
                ),
                showarrow=False,
                xref="paper",
                yref="paper",
                x=0.5,
                y=-0.15,
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
