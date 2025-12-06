from typing import TypedDict


class _Candidate(TypedDict):
    prob: float
    route: str   # WKT MULTILINESTRING
    point: str   # WKT POINT


class _StateMessage(TypedDict):
    id: str
    time: int
    point: str
    route: str
    candidates: list[_Candidate]