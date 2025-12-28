from typing import TypedDict, List


class _Sample(TypedDict):
    id: str
    time: int
    point: str  # WKT POINT(lon lat)


class _BarefootGeoJSONResponse(TypedDict):
    type: str  # "MultiLineString"
    coordinates: List[
        List[List[float]]
    ]  # Lista de linhas, onde cada linha é uma lista de [lon, lat]
    path_osm_ids: List[int]


class _BarefootOfflineRequest(TypedDict):
    format: str
    request: List[_Sample]
