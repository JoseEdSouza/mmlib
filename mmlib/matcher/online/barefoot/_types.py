from typing import TypedDict

class _PointMessage(TypedDict):
    id: str        # Identificador único do veículo/objeto
    time: int      # Timestamp (geralmente milissegundos)
    point: str     # Formato WKT: "POINT(longitude latitude)"

class _Candidate(TypedDict):
    prob: float
    route: str      # WKT MULTILINESTRING da rota percorrida
    point: str      # WKT POINT da posição projetada na via
   
class _StateMessage(TypedDict):
    id: str
    time: int       # Timestamp da última amostra
    point: str      # WKT POINT (amostra original)
    osm_id: int     
    osm_type: str
    edge_gid: int
    source: int
    target: int
    candidates: list[_Candidate]