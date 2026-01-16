from typing import TypedDict


class _PointMessage(TypedDict):
    id: str  # Unique identifier for the vehicle/object
    time: float  # Timestamp (usually milliseconds)
    point: str  # WKT format: "POINT(longitude latitude)"


class _Candidate(TypedDict):
    prob: float
    route: str  # WKT MULTILINESTRING of the traveled route
    point: str  # WKT POINT for the position projected onto the road


class _StateMessage(TypedDict):
    id: str
    time: float  # Timestamp of the last sample
    point: str  # WKT POINT (original sample)
    osm_id: int
    path_osm_ids: list[int]
    osm_type: str
    edge_gid: int
    source: int
    target: int
    candidates: list[_Candidate]
