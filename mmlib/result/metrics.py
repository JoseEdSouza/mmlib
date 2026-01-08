from dataclasses import dataclass, field
from typing import Any, Iterable
from uuid import uuid4
import networkx as nx


@dataclass(frozen=True)
class MatchMetrics:
    """topologic and distance-based map matching evaluation metrics."""

    precision: float
    recall: float
    f1_score: float
    accuracy: float

    # Based on hidden-markov-map-matching-through-noise-and-sparseness newson & krumm 2009
    newson_krumm_error: float | None = None

    # Counts
    matched_count: int = 0
    added_count: int = 0
    missing_count: int = 0
    ground_truth_count: int = 0

    # Distances (if graph provided)
    total_gt_length: float | None = None
    total_added_length: float | None = None
    total_missing_length: float | None = None

    run_id: str = field(default_factory=lambda: uuid4().hex)

    # Based on spatio-temporal-trajectory-simplification-for-inferring-travel-paths li et. al. 2014
    @property
    def error_rate(self) -> float:
        """Calculate the error rate as 1 - F1 Score."""
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
        """Calculate map matching evaluation metrics."""
        return calculate_match_metrics(
            run_id=run_id,
            ground_truth_edges=ground_truth_edges,
            matched_edges=matched_edges,
            graph=graph,
        )


def _calculate_total_length(
    edge_ids: Iterable[str], osmid_to_len: dict[str, float]
) -> float:
    """Calculate the total length of a set of edge IDs."""
    return sum(osmid_to_len.get(str(eid), 0.0) for eid in edge_ids)


def calculate_match_metrics(
    ground_truth_edges: Iterable[str],
    matched_edges: Iterable[str],
    run_id: str | None = None,
    graph: nx.Graph | nx.MultiDiGraph | None = None,
) -> MatchMetrics:
    """
    Calculate map matching evaluation metrics.

    Args:
        ground_truth_edges: An iterable of ground truth OSM IDs.
        matched_edges: An iterable of map-matched OSM IDs.
        graph: Optional networkx graph to calculate distance-based metrics (Newson & Krumm).

    Returns:
        MatchMetrics: A dataclass containing the calculated metrics.
    """
    gt_set = set(str(e) for e in ground_truth_edges)
    mm_set = set(str(e) for e in matched_edges)

    matched = gt_set & mm_set
    added = mm_set - gt_set
    missing = gt_set - mm_set

    precision = len(matched) / len(mm_set) if mm_set else 0.0
    recall = len(matched) / len(gt_set) if gt_set else 0.0
    f1 = (
        2 * (precision * recall) / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    # Accuracy based on Jaccard Index (Intersection over Union)
    union = gt_set | mm_set
    accuracy = len(matched) / len(union) if union else 1.0

    # Distance-based metrics
    nk_error = None
    total_gt_len = None
    total_added_len = None
    total_missing_len = None

    if graph is not None:
        # Map OSMID to length (OSMnx graphs have 'length' attribute on edges)
        osmid_to_len: dict[str, float] = {}
        for _, _, data in graph.edges(data=True):
            osm_ids = data.get("osmid")
            length = data.get("length", 0.0)

            if isinstance(osm_ids, (list, tuple, set)):
                for oid in osm_ids:
                    oid_str = str(oid)
                    # Use max length if an OSMID appears in multiple edges?
                    # Usually an OSMID maps to one or more edges forming the same way.
                    # This is a pragmatic approximation.
                    osmid_to_len[oid_str] = max(osmid_to_len.get(oid_str, 0.0), length)
            elif osm_ids is not None:
                oid_str = str(osm_ids)
                osmid_to_len[oid_str] = max(osmid_to_len.get(oid_str, 0.0), length)

        total_gt_len = _calculate_total_length(gt_set, osmid_to_len)
        total_added_len = _calculate_total_length(added, osmid_to_len)
        total_missing_len = _calculate_total_length(missing, osmid_to_len)

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
