import json
from typing import Any

import polyline
import requests

from mmlib.gpx import GPSPoint, to_gpx
from mmlib.matcher import Coordinate, MatchResult
from mmlib.matcher import BaseMatcher


class Matcher(BaseMatcher):
    _base_url: str
    _gps_accuracy: int

    def __init__(self, base_url: str, gps_accuracy: int | None = None) -> None:
        self._base_url = base_url

        self._gps_accuracy = 50
        if gps_accuracy is not None:
            self._gps_accuracy = gps_accuracy

    def map_match(self, points: list[GPSPoint]) -> MatchResult:
        gpx_points = to_gpx(points)
        response = self.__request(gpx_points)
        res_points = polyline.decode(response["points"])
        edge_ids = [str(edge) for (_, __, edge) in response["edge_ids"]]
        return MatchResult.__from_internal(
            matcher_name="GraphHopper",
            measurement_points=[
                Coordinate(latitude=lat, longitude=lon) for lat, lon, _ in points
            ],
            matched_points=[Coordinate(latitude=lat, longitude=lon) for lat, lon in res_points],
            edge_ids=edge_ids,
        )

    def __request(self, gpx_points: str) -> dict[str, Any]:
        url = f"{self._base_url}/match?profile=car&gps_accuracy={self._gps_accuracy}&type=json&locale=pt_BR&details=osm_way_id"
        headers = {
            "Content-Type": "application/gpx+xml",
        }

        req = requests.post(
            url,
            headers=headers,
            data=gpx_points.encode("utf-8"),
        )

        req.raise_for_status()

        res = json.loads(req.content)["paths"][0]

        return {
            "points": res["points"],
            "edge_ids": res["details"]["osm_way_id"],
        }


def graphhopper_matcher(base_url: str) -> Matcher:
    return Matcher(base_url=base_url)
