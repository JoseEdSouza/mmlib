from datetime import datetime
from typing import NamedTuple


class GPSPoint(NamedTuple):
    """
    Type alias for GPS coordinates with the timestamp.
    Follows the RFC 3339 format for timestamps and the WGS84 standard for coordinates.
    (latitude, longitude, timestamp)
    """

    lat: float
    lon: float
    time: datetime

    @classmethod
    def from_wkt_timestamp(cls, wkt: str, timestamp: float) -> "GPSPoint":
        lat, lon = wkt.split(",")[1:3]
        return cls(float(lat), float(lon), datetime.fromtimestamp(timestamp))

    @property
    def as_tuple(self) -> tuple[float, float, datetime]:
        return self.lat, self.lon, self.time

    @property
    def coordinate(self) -> "Coordinate":
        return Coordinate(self.lat, self.lon)


class Coordinate(NamedTuple):
    """
    Type alias for GPS coordinates.
    Follows the WGS84 standard for coordinates.
    (latitude, longitude)
    """

    lat: float
    lon: float

    @property
    def as_tuple(self) -> tuple[float, float]:
        return self.lat, self.lon
