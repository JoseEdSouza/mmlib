import asyncio
import json
import logging
from typing import AsyncIterable, AsyncIterator, override, Self

import networkx as nx
import osmnx as ox
import zmq
import zmq.asyncio as azmq
from shapely import wkt

from mmlib.matcher.base import BaseOnlineMatcher
from mmlib.result import OnlineMatchResult
from mmlib.types import GPSPoint
from mmlib.types.points import Coordinate
from ._types import _StateMessage
from mmlib.exceptions import (
    MatcherConnectionError,
    MatcherTimeoutError,
    MatcherProtocolError,
)
from mmlib.utils import factory

logger = logging.getLogger(__name__)


class BarefootMatcher(BaseOnlineMatcher):
    _matcher_name = "barefoot"

    def __init__(
        self,
        G_road: nx.MultiDiGraph,
        host: str = "localhost",
        port: int = 1234,
        broker_url: str = "tcp://localhost:5556",
        vehicle_id: str | None = None,
        timeout: float = 5.0,
    ):
        self._host = host
        self._port = port
        self._broker_url = broker_url
        self._vehicle_id = vehicle_id or "default_vehicle"
        self._timeout = timeout

        self._G_road = G_road

        # State managed by Context Manager
        self._ctx: azmq.Context | None = None
        self._socket: azmq.Socket | None = None
        self._send_queue: asyncio.Queue[GPSPoint | None] = asyncio.Queue()

        self._result: OnlineMatchResult = OnlineMatchResult(
            matcher_name=self._matcher_name
        )

    @override
    async def __aenter__(self) -> Self:
        """Initialize resources (ZMQ context and socket)."""
        if self._ctx is not None:
            return self

        try:
            self._ctx = azmq.Context()
            self._socket = self._ctx.socket(zmq.SUB)
            self._socket.connect(self._broker_url)
            self._socket.setsockopt(zmq.SUBSCRIBE, b"")
            logger.info(f"Connected to ZMQ broker at {self._broker_url}")
        except Exception as e:
            raise MatcherConnectionError(
                f"Failed to connect to ZMQ broker at {self._broker_url}"
            ) from e
        return self

    @override
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Clean up resources."""
        if self._socket:
            self._socket.close()
            self._socket = None
        if self._ctx:
            self._ctx.term()
            self._ctx = None
        logger.info("Disconnected from ZMQ broker")

    async def _writer_loop(self, points: AsyncIterable[GPSPoint]) -> None:
        """Background task to send points to Barefoot TCP server."""
        try:
            async for pt in points:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(self._host, self._port),
                        timeout=self._timeout,
                    )
                except asyncio.TimeoutError:
                    logger.error(f"Timeout connecting to {self._host}:{self._port}")
                    continue
                except OSError as e:
                    logger.error(
                        f"Failed to connect to Barefoot TCP at {self._host}:{self._port}: {e}"
                    )
                    continue

                try:
                    msg = json.dumps(
                        {
                            "id": self._vehicle_id,
                            "time": int(pt.time.timestamp() * 1000),
                            "point": f"POINT({pt.coordinate.lon} {pt.coordinate.lat})",
                        }
                    )
                    writer.write(msg.encode() + b"\n")
                    if writer.can_write_eof():
                        writer.write_eof()
                    await writer.drain()

                    # Read ACK "SUCCESS"
                    response = await asyncio.wait_for(
                        reader.read(), timeout=self._timeout
                    )
                    logger.debug(f"Barefoot response: {response}")

                    if b"SUCCESS" not in response:
                        raise MatcherProtocolError(
                            f"Barefoot matching failed for point {pt}: {response.decode()}"
                        )

                except asyncio.TimeoutError:
                    logger.error("Timeout waiting for Barefoot response")
                    continue
                except MatcherProtocolError as e:
                    logger.error(str(e))
                    continue
                except Exception as e:
                    logger.error(f"Unexpected error in writer loop: {e}")
                    continue
                finally:
                    writer.close()
                    await writer.wait_closed()

                await self._send_queue.put(pt)

        except Exception:
            logger.exception("Critical error in writer loop")
            raise
        finally:
            # Signal end of stream
            await self._send_queue.put(None)

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """
        Yields match results as they arrive.
        Requires the matcher to be used within an `async with` block.
        """
        if self._socket is None:
            raise RuntimeError(
                "Matcher used outside of async context. Use 'async with matcher:'"
            )

        # Start the writer task
        writer_task = asyncio.create_task(self._writer_loop(points))

        try:
            while True:
                pt = await self._send_queue.get()

                if pt is None:
                    break

                try:
                    raw = await asyncio.wait_for(
                        self._socket.recv(), timeout=self._timeout
                    )

                    data: _StateMessage = json.loads(raw.decode())

                    # Update result
                    (x, y) = wkt.loads(data["point"]).coords[0]
                    coordinate = Coordinate(lon=x, lat=y)
                    edge_id = self._snap_matched_point_to_edge(coordinate)

                    self._result._update(
                        new_point=pt,
                        matched_point=coordinate,
                        edge_id=edge_id,
                    )

                    yield self._result

                except asyncio.TimeoutError as e:
                    logger.error("Timeout waiting for ZMQ message")
                    raise MatcherTimeoutError("Timeout waiting for ZMQ message") from e
                except Exception as e:
                    logger.error(f"Error processing ZMQ message: {e}")

        finally:
            # Ensure writer task is cleaned up if we break early
            if not writer_task.done():
                writer_task.cancel()
                try:
                    await writer_task
                except asyncio.CancelledError:
                    pass

    def _snap_matched_point_to_edge(self, point: Coordinate) -> str:
        u, v, k = ox.nearest_edges(self._G_road, point.lon, point.lat)
        return self._G_road.edges[u, v, k]["osmid"]


@factory(BarefootMatcher)
def barefoot_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return BarefootMatcher(*args, **kwargs)
