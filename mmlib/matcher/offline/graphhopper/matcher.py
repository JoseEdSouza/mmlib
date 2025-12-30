import json
from typing import Any, Final

import polyline
import requests

from mmlib.matcher.base import BaseMatcher
from mmlib.result import MatchResult
from mmlib.utils.gpx import to_gpx
from mmlib.types.points import GPSPoint, Coordinate
from mmlib.utils import factory


class GraphHopperMatcher(BaseMatcher):
    """Offline matcher using GraphHopper Map Matching API."""

    _matcher_name: Final[str] = "graphhopper"

    def __init__(
        self,
        base_url: str,
        gps_accuracy: int = 50,
        profile: str = "car",
        locale: str = "pt_BR",
    ) -> None:
        self._base_url = base_url
        self._gps_accuracy = gps_accuracy
        self._profile = profile
        self._locale = locale

    @property
    def matcher_name(self) -> str:
        return self._matcher_name

    def match(self, points: list[GPSPoint]) -> MatchResult:
        gpx_points = to_gpx(points)
        response = self._request(gpx_points)
        res_points = polyline.decode(response["points"])
        edge_ids = [str(edge) for (_, __, edge) in response["edge_ids"]]
        return MatchResult(
            matcher_name="GraphHopper",
            measurement_points=[GPSPoint(lat, lon, time) for lat, lon, time in points],
            matched_points=[Coordinate(lat, lon) for lat, lon in res_points],
            edge_ids=edge_ids,
        )

    def _request(self, gpx_points: str) -> dict[str, Any]:
        url = (
            f"{self._base_url}/match"
            f"?profile={self._profile}"
            f"&gps_accuracy={self._gps_accuracy}"
            f"&type=json"
            f"&locale={self._locale}"
            f"&details=osm_way_id"
        )
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


@factory(GraphHopperMatcher)
def graphhopper_matcher(*args, **kwargs) -> BaseMatcher:
    return GraphHopperMatcher(*args, **kwargs)
