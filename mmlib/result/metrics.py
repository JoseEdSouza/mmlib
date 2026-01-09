import pandas as pd
import osmnx as ox
import networkx as nx
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable
from uuid import uuid4

from mmlib.config import config
from mmlib.exceptions import MatcherInputError

# Global registry to maintain references to graphs
_cached_graphs: dict[int, nx.MultiDiGraph | nx.Graph] = {}


@dataclass(frozen=True)
class MatchMetrics:
    """Topologic and distance-based map matching evaluation metrics."""

    precision: float
    recall: float
    f1_score: float
    accuracy: float

    # Based on Newson & Krumm 2009
    newson_krumm_error: float | None = None

    # Counts
    matched_count: int = 0
    added_count: int = 0
    missing_count: int = 0
    ground_truth_count: int = 0

    # Distances
    total_gt_length: float | None = None
    total_added_length: float | None = None
    total_missing_length: float | None = None

    run_id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def error_rate(self) -> float:
        """Calculate error rate as complement of F1 score."""
        return 1.0 - self.f1_score

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to a dictionary."""
        return {
            "run_id": self.run_id,
            "precision": self.precision,
            "recall": self.recall,
            "f1_score": self.f1_score,
            "error_rate": self.error_rate,
            "accuracy": self.accuracy,
            "newson_krumm_error": self.newson_krumm_error,
            "matched_count": self.matched_count,
            "added_count": self.added_count,
            "missing_count": self.missing_count,
            "ground_truth_count": self.ground_truth_count,
            "total_gt_length": self.total_gt_length,
            "total_added_length": self.total_added_length,
            "total_missing_length": self.total_missing_length,
        }

    @staticmethod
    def calculate(
        ground_truth_edges: Iterable[str],
        matched_edges: Iterable[str],
        run_id: str | None = None,
        graph: nx.Graph | nx.MultiDiGraph | None = None,
    ) -> "MatchMetrics":
        """Calculate match metrics using the provided edges and optional graph."""
        return calculate_match_metrics(
            run_id=run_id,
            ground_truth_edges=ground_truth_edges,
            matched_edges=matched_edges,
            graph=graph,
        )


def _calculate_total_length(edge_ids: set[str], osmid_to_len: pd.Series) -> float:
    """Calculate total length in vectorized form via Pandas."""
    if edge_ids:
        # reindex fetches all IDs at once; fillna handles IDs not found in graph
        return osmid_to_len.reindex(list(edge_ids)).fillna(0.0).sum()
    return 0.0


def _process_graph_logic(graph: nx.Graph | nx.MultiDiGraph) -> pd.Series:
    """
    Transform Graph into Series of osmid to length using OSMnx and GeoPandas.

    The graph must have a 'crs' attribute in its global graph dictionary to be
    processed by OSMnx. If the CRS is not projected, the function attempts to
    estimate a UTM projection for accurate distances. If estimation fails,
    it falls back to WGS84 (EPSG:4326).

    Args:
        graph: NetworkX graph (Graph or MultiDiGraph).

    Returns:
        pd.Series: Mapping of osmid (str) to its maximum length (float).

    Raises:
        MatcherInputError: If the graph does not have a 'crs' attribute.
    """
    if "crs" not in graph.graph:
        raise MatcherInputError(
            "Graph must have a 'crs' attribute in its global graph dictionary "
            "to be processed by OSMnx. If this is a mock graph, set "
            "graph.graph['crs'] = 'epsg:4326' or similar."
        )

    # Convert Graph to MultiDiGraph if needed (required by OSMnx)
    if isinstance(graph, nx.Graph) and not isinstance(graph, nx.MultiGraph):
        graph = nx.MultiDiGraph(graph)

    # Extract edges to GeoDataFrame
    gdf_edges = ox.graph_to_gdfs(graph, nodes=False, fill_edge_geometry=False)

    # If not projected, try to project to UTM for accurate distance-based metrics
    if gdf_edges.crs is not None and not gdf_edges.crs.is_projected:
        try:
            utm_crs = gdf_edges.estimate_utm_crs()
            gdf_edges = gdf_edges.to_crs(utm_crs)
        except Exception:
            # Fallback to EPSG:4326 if estimation fails
            if gdf_edges.crs != "epsg:4326":
                gdf_edges = gdf_edges.to_crs("epsg:4326")

    # Explode osmid and extract length per edge
    gdf_exploded = gdf_edges[["osmid", "length"]].explode("osmid")
    gdf_exploded["osmid"] = gdf_exploded["osmid"].astype(str)

    return gdf_exploded.groupby("osmid")["length"].max()


@lru_cache(maxsize=config.cache_size if config.use_cache else 1)
def _process_graph_cached(graph_id: int) -> pd.Series:
    """Retrieve graph from registry and process with caching."""
    graph = _cached_graphs[graph_id]
    return _process_graph_logic(graph)


def calculate_match_metrics(
    ground_truth_edges: Iterable[str],
    matched_edges: Iterable[str],
    run_id: str | None = None,
    graph: nx.Graph | nx.MultiDiGraph | None = None,
) -> MatchMetrics:
    """Calculate matching metrics comparing ground truth and matched edges."""
    # Initial normalization
    gt_set = {str(e) for e in ground_truth_edges}
    mm_set = {str(e) for e in matched_edges}

    # Compute edge sets
    matched = gt_set & mm_set
    added = mm_set - gt_set
    missing = gt_set - mm_set

    # Calculate metrics
    precision = len(matched) / len(mm_set) if mm_set else 0.0
    recall = len(matched) / len(gt_set) if gt_set else 0.0
    f1 = (
        (2 * precision * recall / (precision + recall))
        if (precision + recall) > 0
        else 0.0
    )
    accuracy = len(matched) / len(gt_set | mm_set) if (gt_set | mm_set) else 1.0

    nk_error = None
    total_gt_len = None
    total_added_len = None
    total_missing_len = None

    # Calculate distance-based metrics if graph is provided
    if graph is not None:
        if config.use_cache:
            graph_id = id(graph)
            if graph_id not in _cached_graphs:
                _cached_graphs[graph_id] = graph
            osmid_to_len = _process_graph_cached(graph_id)
        else:
            osmid_to_len = _process_graph_logic(graph)

        total_gt_len = _calculate_total_length(gt_set, osmid_to_len)
        total_added_len = _calculate_total_length(added, osmid_to_len)
        total_missing_len = _calculate_total_length(missing, osmid_to_len)

        # Calculate Newson & Krumm error
        if total_gt_len > 0:
            nk_error = (total_added_len + total_missing_len) / total_gt_len
        else:
            nk_error = 0.0 if not mm_set else float("inf")

    return MatchMetrics(
        run_id=run_id or uuid4().hex,
        precision=precision,
        recall=recall,
        f1_score=f1,
        accuracy=accuracy,
        newson_krumm_error=nk_error,
        matched_count=len(matched),
        added_count=len(added),
        missing_count=len(missing),
        ground_truth_count=len(gt_set),
        total_gt_length=total_gt_len,
        total_added_length=total_added_len,
        total_missing_length=total_missing_len,
    )


def clear_mmlib_graph_cache():
    """Clear the internal cache used by mmlib for graph processing."""
    _process_graph_cached.cache_clear()
    _cached_graphs.clear()
