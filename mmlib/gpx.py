from datetime import datetime
from typing import  Sequence

from gpx.gpx import GPX
from gpx.waypoint import Waypoint
from gpx.track import Track
from gpx.track_segment import TrackSegment
from gpx.types import Latitude, Longitude
from typing import NamedTuple

"""
Type alias for GPS coordinates with timestamp
follows the RFC 3339 format for timestamps
(latitude, longitude, timestamp)
"""
class GPSPoint(NamedTuple):
    lat: float
    lon: float
    time: datetime

def __create_waypoint(point: GPSPoint) -> Waypoint:
    w = Waypoint()
    lat, lon, time = point
    w.lon = Longitude(lon)
    w.lat = Latitude(lat)
    w.time = time
    return w


def __to_track(points: Sequence[GPSPoint]) -> Track:
    wps = [__create_waypoint(p) for p in points]

    ts = TrackSegment()
    ts.points.extend(wps)

    trk = Track()
    trk.trksegs.append(ts)

    return trk  


# receives a list of GPS points and returns a GPX object
# each GPS point is a tuple (latitude, longitude, timestamp)
def to_gpx(points: Sequence[GPSPoint]) -> str:
    """
    Convert waypoints, tracks, and routes to a GPX object.

    :param waypoints: List of waypoints.
    :return: GPX object as a string.
    """
    gpx = GPX()

    trk = __to_track(points)

    gpx.tracks.append(trk)

    return gpx.to_string()
