import json
import logging
from typing import Any, override

import requests
from shapely import wkt

from mmlib.matcher.base import BaseMatcher
from mmlib.result.offline import MatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory

logger = logging.getLogger(__name__)


class GraphiumOfflineMatcher(BaseMatcher):
    """
    Offline matcher using the Graphium API.
    """

    def __init__(
        self,
        base_url: str,
        graph_name: str,
        version: str = "current",
        timeout_s: float = 60.0,
    ) -> None:
        super().__init__()
        self._base_url = base_url
        self._graph_name = graph_name
        self._version = version

        if timeout_s <= 0:
            raise ValueError("Timeout must be a positive value.")

        self._timeout_ms = int(timeout_s * 1000)

    @property
    def matcher_name(self) -> str:
        """The name of the matcher."""
        return "GraphiumOfflineMatcher"

    @override
    def match(self, points: list[GPSPoint]) -> MatchResult:
        """
        Map match the provided GPS points using Graphium.

        Args:
            points (list[GPSPoint]): The GPS points to map match.

        Returns:
            MatchResult: The result of the map matching process.
        """
        response = self._request(points)
        res_points = response["points"]
        edge_ids = response["edge_ids"]
        return MatchResult(
            matcher_name=self.matcher_name,
            measurement_points=[
                GPSPoint(lat=lat, lon=lon, time=ts) for lat, lon, ts in points
            ],
            matched_points=res_points,
            edge_ids=edge_ids,
        )

    def match_with_extra_params(
        self,
        points: list[GPSPoint],
        extra_params: dict[str, Any] | None = None,
        session: requests.Session | None = None,
    ) -> tuple[MatchResult, str]:
        """Map match the provided GPS points using Graphium with extra parameters."""
        response = self._request(points, extra_params, session=session)
        res_points = response["points"]
        edge_ids = response["edge_ids"]
        return (
            MatchResult(
                matcher_name=self.matcher_name,
                measurement_points=[
                    GPSPoint(lat=lat, lon=lon, time=ts) for lat, lon, ts in points
                ],
                matched_points=res_points,
                edge_ids=edge_ids,
            ),
            response["last_segment_id"],
        )

    def _request(
        self,
        points: list[GPSPoint],
        extra_params: dict[str, Any] | None = None,
        session: requests.Session | None = None,
    ) -> dict:
        """
        Send a request to the Graphium matching API.

        Args:
            points (list[GPSPoint]): The points to match.

        Returns:
            dict: The processed response containing matched points and edge IDs.
        """

        url = f"{self._base_url}/matching/graphs/{self._graph_name}/versions/{self._version}/matchtrack"
        params = {"outputVerbose": False, "timeoutMs": self._timeout_ms}
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if extra_params:
            params.update(extra_params)

        track_points = [
            {
                "id": i,
                "timestamp": int(ts.timestamp() * 1000),
                "x": lon,
                "y": lat,
                "z": 0,
            }
            for i, (lat, lon, ts) in enumerate(points)
        ]

        payload = {"id": 1, "trackPoints": track_points}

        post_func = session.post if session else requests.post

        response = post_func(url, params=params, json=payload, headers=headers)

        if not response.ok:
            logger.error(f"Request failed ({response.status_code}): {response.text}")
            response.raise_for_status()

        res = json.loads(response.content)
        segments = res.get("segments", [])

        all_geometries = [wkt.loads(seg["geometry"]) for seg in segments]
        all_coordinates = [
            Coordinate(lat, lon) for geom in all_geometries for lon, lat in geom.coords
        ]
        all_edge_ids = [str(seg["wayId"]) for seg in segments]
        last_segment_id = segments[-1]["segmentId"] if segments else None

        return {
            "points": all_coordinates,
            "edge_ids": all_edge_ids,
            "last_segment_id": last_segment_id,
        }


@factory(GraphiumOfflineMatcher)
def graphium_offline_matcher(*args, **kwargs) -> GraphiumOfflineMatcher:
    return GraphiumOfflineMatcher(*args, **kwargs)
