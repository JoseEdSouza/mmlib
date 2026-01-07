from dataclasses import dataclass
from datetime import datetime


from mmlib.types import Coordinate, GPSPoint
from mmlib.result.base import BaseMatchResult


@dataclass
class OnlineMatchResult(BaseMatchResult):
    def _update_sent(self, new_point: GPSPoint | Coordinate | None):
        """Updates state by adding measurement points."""
        if new_point:
            if isinstance(new_point, GPSPoint):
                self.measurement_points.append(new_point)
            else:
                self.measurement_points.append(
                    GPSPoint(lat=new_point.lat, lon=new_point.lon, time=datetime.now())
                )

    def _update_matched(
        self,
        matched_point: Coordinate | None = None,
        edge_id: str | None = None,
    ):
        """Updates state by adding matched points and edge IDs."""
        if matched_point:
            self.matched_points.append(matched_point)

        if edge_id:
            self.edge_ids.append(edge_id)
