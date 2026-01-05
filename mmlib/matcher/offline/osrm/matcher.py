import logging
from typing import Any, Final, override

import requests

from mmlib.matcher.base import BaseMatcher
from mmlib.result import MatchResult
from mmlib.types import Coordinate, GPSPoint
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

        matchings = response_data.get("matchings", [])
        if not matchings:
            logger.warning("OSRM returned 'Ok' code but no matchings found.")
            return MatchResult(
                matcher_name=self.matcher_name,
                measurement_points=points,
                matched_points=[],
                edge_ids=[],
            )

        best_match = matchings[0]

        matched_points: list[Coordinate] = []
        geometry = best_match.get("geometry")

        if geometry and geometry.get("type") == "LineString":
            coords = geometry.get("coordinates", [])
            matched_points = [Coordinate(lat=c[1], lon=c[0]) for c in coords]

        edge_ids: list[str] = []
        for leg in best_match.get("legs", []):
            for step in leg.get("steps", []):
                way_id = step.get("name", "")
                edge_ids.append(str(way_id))

        return MatchResult(
            matcher_name=self.matcher_name,
            measurement_points=points,
            matched_points=matched_points,
            edge_ids=edge_ids,
        )

    def _request(self, points: list[GPSPoint]) -> dict[str, Any]:
        # Format coordinates: {lon},{lat};{lon},{lat}
        coordinates = ";".join(f"{p.coordinate.lon},{p.coordinate.lat}" for p in points)

        # Format timestamps: Unix epoch integers
        timestamps = ";".join(str(int(p.time.timestamp())) for p in points)

        url = f"{self._base_url}/match/v1/{self._profile}/{coordinates}"

        params = {
            "timestamps": timestamps,
            "geometries": "geojson",
            "overview": "full",
            "steps": "true",  # Required to get the 'name' field per step
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
