import networkx as nx
from mmlib.result.metrics import calculate_match_metrics


def test_metrics():
    # Setup mock data
    gt_edges = ["1", "2", "3"]
    mm_edges = ["2", "3", "4"]  # "1" missing, "4" added

    # Create mock graph with lengths
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "epsg:4326"
    graph.add_edge(1, 2, osmid="1", length=100.0)
    graph.add_edge(2, 3, osmid="2", length=200.0)
    graph.add_edge(3, 4, osmid="3", length=300.0)
    graph.add_edge(4, 5, osmid="4", length=50.0)

    # Calculate metrics
    metrics = calculate_match_metrics(gt_edges, mm_edges, graph=graph)

    print(f"Metrics: {metrics.to_dict()}")

    # Topologic assertions (Length-based)
    # Matched: ["2", "3"] -> length 200 + 300 = 500
    # Ground Truth: ["1", "2", "3"] -> length 100 + 200 + 300 = 600
    # Predicted: ["2", "3", "4"] -> length 200 + 300 + 50 = 550
    # Precision: 500 / 550 = 0.9090...
    # Recall: 500 / 600 = 0.8333...
    # Accuracy (IoU): 500 / (600 + 550 - 500) = 500 / 650 = 0.7692...

    assert abs(metrics.precision - 500 / 550) < 0.001
    assert abs(metrics.recall - 500 / 600) < 0.001
    assert abs(metrics.accuracy - 500 / 650) < 0.001

    # Distance assertions
    # Total GT length: 100 + 200 + 300 = 600
    # Added length: 50 (edge "4")
    # Missing length: 100 (edge "1")
    # NK Error: (50 + 100) / 600 = 150 / 600 = 0.25

    assert metrics.total_gt_length == 600.0
    assert metrics.total_added_length == 50.0
    assert metrics.total_missing_length == 100.0
    assert metrics.newson_krumm_error == 0.25

    print("Test passed!")


if __name__ == "__main__":
    test_metrics()
