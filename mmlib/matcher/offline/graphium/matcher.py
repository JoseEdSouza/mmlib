from dataclasses import dataclass
import json
import logging
from typing import Any, TypedDict, override
from uuid import uuid4

import requests
from shapely import wkt

from mmlib.exceptions import (
    MatcherConfigurationError,
    MatcherConnectionError,
    MatcherProtocolError,
)
from mmlib.matcher.base import BaseMatcher
from mmlib.result.offline import MatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory

logger = logging.getLogger(__name__)


class _ParsedSegment(TypedDict):
    id: str
    coords: list[Coordinate]
    way_id: str


@dataclass
class _DetailedMatchResult:
    points: list[Coordinate]
    edge_ids: list[str]
    last_segment_id: str | None
    parsed_segments: list[_ParsedSegment]

    def to_match_result(
        self, matcher_name: str, measurement_points: list[GPSPoint]
    ) -> MatchResult:
        return MatchResult(
            matcher_name=matcher_name,
            measurement_points=measurement_points,
            matched_points=self.points,
            edge_ids=self.edge_ids,
        )


class GraphiumOfflineMatcher(BaseMatcher):
    """
    Offline matcher using the Graphium API.
    """

    @property
    @override
    def matcher_name(self) -> str:
        """The name of the matcher."""
        return "graphium_offline"

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
        self._id = hash(uuid4().hex)

        if timeout_s <= 0:
            raise MatcherConfigurationError("Timeout must be a positive value.")

        self._timeout_ms = int(timeout_s * 1000)
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._params = {
            "outputVerbose": "false",
            "timeoutMs": str(self._timeout_ms),
        }
        self._url = f"{self._base_url}/matching/graphs/{self._graph_name}/versions/{self._version}/matchtrack"


    @override
    def match(self, points: list[GPSPoint]) -> MatchResult:
        """
        Map match the provided GPS points using Graphium.
        """
        response = self._request(points)
        return response.to_match_result(self.matcher_name, points)

    def _match_with_extra_params(
        self,
        points: list[GPSPoint],
        extra_params: dict[str, Any] | None = None,
        session: requests.Session | None = None,
    ) -> _DetailedMatchResult:
        """
        Map match with extra parameters (e.g., startSegmentId).

        Returns:
            tuple: (MatchResult, last_segment_id, parsed_segments)
        """
        response = self._request(points, extra_params, session=session)

        return response

    def _request(
        self,
        points: list[GPSPoint],
        extra_params: dict[str, Any] | None = None,
        session: requests.Session | None = None,
    ) -> _DetailedMatchResult:
        """
        Sends the matching request to the Graphium API and processes the response.
        """

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

        payload = {"id": self._id, "trackPoints": track_points}
        params = self._params | (extra_params or {})
        post = session.post if session else requests.post

        try:
            response = post(
                self._url, params=params, json=payload, headers=self._headers
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as e:
            logger.error(f"Request timeout para Graphium: {e}")
            raise MatcherConnectionError(f"Timeout connecting to Graphium: {e}") from e
        except requests.exceptions.RequestException as e:
            logger.error(f"Request falhou: {e}")
            raise MatcherConnectionError(f"Failed to connect to Graphium: {e}") from e

        if not response.content:
            return _DetailedMatchResult(
                points=[],
                edge_ids=[],
                last_segment_id=None,
                parsed_segments=[],
            )

        try:
            res = json.loads(response.content)
        except json.JSONDecodeError as e:
            logger.error(f"Erro ao decodificar JSON do Graphium: {e}")
            raise MatcherProtocolError(
                f"Invalid JSON response from Graphium: {e}"
            ) from e
        segments = res.get("segments", [])

        all_coordinates: list[Coordinate] = []
        all_edge_ids: list[str] = []
        parsed_segments: list[_ParsedSegment] = []

        for seg in segments:
            geom_wkt = seg.get("geometry")
            coords = []
            if geom_wkt:
                geom = wkt.loads(geom_wkt)
                coords = [Coordinate(lat, lon) for lon, lat in geom.coords]

            seg_id = str(seg.get("segmentId"))
            way_id = str(seg.get("wayId"))

            parsed_segments.append(
                {
                    "id": seg_id,
                    "coords": coords,
                    "way_id": way_id,
                }
            )

            all_coordinates.extend(coords)
            all_edge_ids.append(way_id)

        last_segment_id = segments[-1]["segmentId"] if segments else None

        return _DetailedMatchResult(
            points=all_coordinates,
            edge_ids=all_edge_ids,
            last_segment_id=last_segment_id,
            parsed_segments=parsed_segments,
        )


@factory(GraphiumOfflineMatcher)
def graphium_offline_matcher(*args, **kwargs) -> GraphiumOfflineMatcher:
    return GraphiumOfflineMatcher(*args, **kwargs)
