import json
import socket
import logging
from typing import Final, Optional, cast

from mmlib.matcher.base import BaseMatcher
from mmlib.matcher.offline.barefoot._types import (
    _BarefootOfflineRequest,
    _BarefootGeoJSONResponse,
    _Sample,
)
from mmlib.result import MatchResult
from mmlib.types import GPSPoint, Coordinate
from mmlib.utils import factory

logger = logging.getLogger(__name__)


class BarefootOfflineMatcher(BaseMatcher):
    """Offline matcher using Barefoot Matcher Server (TCP socket) with GeoJSON format."""

    _matcher_name: Final[str] = "barefoot_offline"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1234,
        *,
        vehicle_id: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        if vehicle_id is None:
            import uuid

            vehicle_id = str(uuid.uuid4())
        self._vehicle_id = vehicle_id

    def match(self, points: list[GPSPoint]) -> MatchResult:
        payload = self._prepare_payload(points)
        response_data = self._send_request(payload)

        # Parse geojson (MultiLineString) response
        edge_ids = [str(osm_id) for osm_id in response_data.get("path_osm_ids", [])]

        # Extract matched points from MultiLineString coordinates
        # coordinates is List[List[List[float]]] -> List of Lines, each line is List of [lon, lat]
        matched_points: list[Coordinate] = []
        for line in response_data.get("coordinates", []):
            for lon, lat in line:
                coord = Coordinate(lon=lon, lat=lat)
                # Avoid consecutive duplicates if they appear at line boundaries
                if not matched_points or matched_points[-1] != coord:
                    matched_points.append(coord)

        return MatchResult(
            matcher_name=self._matcher_name,
            measurement_points=[
                GPSPoint(pt.coordinate.lat, pt.coordinate.lon, pt.time) for pt in points
            ],
            matched_points=matched_points,
            edge_ids=edge_ids,
        )

    @property
    def matcher_name(self) -> str:
        return self._matcher_name

    def _prepare_payload(self, points: list[GPSPoint]) -> str:
        samples: list[_Sample] = []
        for pt in points:
            samples.append(
                {
                    "id": self._vehicle_id,
                    "time": int(pt.time.timestamp() * 1000),
                    "point": f"POINT({pt.coordinate.lon} {pt.coordinate.lat})",
                }
            )

        request: _BarefootOfflineRequest = {
            "format": "geojson",
            "request": samples,
        }
        return json.dumps(request) + "\n"

    def _send_request(self, payload: str) -> _BarefootGeoJSONResponse:
        output = ""
        try:
            with socket.create_connection((self._host, self._port), timeout=30.0) as s:
                s.sendall(payload.encode("utf-8"))
                s.shutdown(socket.SHUT_WR)

                while True:
                    buf = s.recv(4096)
                    if not buf:
                        break
                    output += buf.decode("utf-8")
        except OSError as e:
            logger.error(
                "Failed to connect to Barefoot server at %s:%s: %s",
                self._host,
                self._port,
                e,
            )
            raise

        if not output:
            raise RuntimeError("Empty response from Barefoot server")

        if output.startswith("SUCCESS\n"):
            output = output[len("SUCCESS\n") :]

        try:
            data = json.loads(output)
            return cast(_BarefootGeoJSONResponse, data)
        except json.JSONDecodeError as e:
            logger.error("Failed to decode Barefoot response: %s", output)
            raise RuntimeError(f"Invalid JSON from Barefoot: {e}") from e


@factory(BarefootOfflineMatcher)
def barefoot_offline_matcher(*args, **kwargs) -> BarefootOfflineMatcher:
    return BarefootOfflineMatcher(*args, **kwargs)
