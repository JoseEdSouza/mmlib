from datetime import datetime
from typing import NamedTuple, Sequence

from gpx.gpx import GPX
from gpx.track import Track
from gpx.track_segment import TrackSegment
from gpx.types import Latitude, Longitude
from gpx.waypoint import Waypoint


class GPSPoint(NamedTuple):
    """
    Type alias for GPS coordinates with timestamp.
    Follows the RFC 3339 format for timestamps.
    (latitude, longitude, timestamp)
    """

    lat: float
    lon: float
    time: datetime


def _create_waypoint(point: GPSPoint) -> Waypoint:
    w = Waypoint()
    lat, lon, time = point
    w.lon = Longitude(lon)
    w.lat = Latitude(lat)
    w.time = time
    return w


def _to_track(points: Sequence[GPSPoint]) -> Track:
    wps = [_create_waypoint(p) for p in points]

    ts = TrackSegment()
    ts.points.extend(wps)

    trk = Track()
    trk.trksegs.append(ts)

    return trk


def to_gpx(points: Sequence[GPSPoint]) -> str:
    """
    Convert a sequence of GPS points to a GPX string.

    Args:
        points: Sequence of GPSPoint (lat, lon, time).

    Returns:
        str: GPX XML string.
    """
    gpx = GPX()

    trk = _to_track(points)

    gpx.tracks.append(trk)

    return gpx.to_string()
