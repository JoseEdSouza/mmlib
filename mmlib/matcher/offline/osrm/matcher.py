import logging
from typing import Any, Final, override

import requests

from mmlib.matcher.base import BaseMatcher
from mmlib.result import MatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory

logger = logging.getLogger(__name__)


class OSRMMatcher(BaseMatcher):
    """
    Offline matcher using the OSRM Match API.

    This implementation assumes OSRM is configured to return OSM Way IDs
    in the 'name' field of the route steps.
    """

    _matcher_name: Final[str] = "osrm"

    def __init__(
        self,
        base_url: str,
        profile: str = "car",
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._profile = profile
        self._timeout = timeout

    @property
    def matcher_name(self) -> str:
        return self._matcher_name

    @override
    def match(self, points: list[GPSPoint]) -> MatchResult:
        if not points:
            return MatchResult(
                matcher_name=self.matcher_name,
                measurement_points=[],
                matched_points=[],
                edge_ids=[],
            )

        response_data = self._request(points)

        all_matched_points: list[Coordinate] = []
        all_edge_ids: list[str] = []

        matchings = response_data.get("matchings", [])

        for matching in matchings:
            geometry = matching.get("geometry")
            if geometry and geometry.get("type") == "LineString":
                # GeoJSON coordinates are [lon, lat]
                coords = geometry.get("coordinates", [])
                segment_points = [Coordinate(lat=c[1], lon=c[0]) for c in coords]
                all_matched_points.extend(segment_points)

            # matching -> legs -> steps -> name
            for leg in matching.get("legs", []):
                for step in leg.get("steps", []):
                    # O Way ID está no campo 'name'
                    way_id = step.get("name", "")
                    all_edge_ids.append(str(way_id))

        if not all_matched_points and response_data.get("code") == "Ok":
            logger.warning(
                "OSRM returned 'Ok' but no geometry extracted from matchings."
            )

        return MatchResult(
            matcher_name=self.matcher_name,
            measurement_points=points,
            matched_points=all_matched_points,
            edge_ids=all_edge_ids,
        )

    def _request(self, points: list[GPSPoint]) -> dict[str, Any]:
        # Formats coords: {lon},{lat};{lon},{lat}
        coordinates = ";".join(f"{lat},{lon}" for (lat, lon, _) in points)

        # Formats timestamps: {ts1};{ts2};...
        timestamps = ";".join(
            str(int(timestamp.timestamp())) for (_, _, timestamp) in points
        )

        url = f"{self._base_url}/match/v1/{self._profile}/{coordinates}"

        params = {
            "timestamps": timestamps,
            "geometries": "geojson",
            "overview": "full",
            "steps": "true",
            "annotations": "nodes,distance,duration,speed",
        }

        try:
            response = requests.get(url, params=params, timeout=self._timeout)
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as e:
            logger.error(f"OSRM request failed: {e}")
            raise RuntimeError(f"Failed to connect to OSRM: {e}") from e

        if result.get("code") != "Ok":
            code = result.get("code")
            msg = result.get("message", "Unknown error")
            logger.error(f"OSRM API returned error {code}: {msg}")
            raise RuntimeError(f"OSRM matching failed: {code} - {msg}")

        return result


@factory(OSRMMatcher)
def osrm_matcher(*args, **kwargs) -> BaseMatcher:
    return OSRMMatcher(*args, **kwargs)
