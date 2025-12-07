import asyncio
import json
import logging
import uuid
from typing import AsyncIterable, AsyncIterator, Final, override, Self

import networkx as nx
import osmnx as ox
import zmq
import zmq.asyncio as azmq
from shapely import wkt

from mmlib.matcher.base import BaseOnlineMatcher
from mmlib.result import OnlineMatchResult
from mmlib.types import GPSPoint, Coordinate
from mmlib.utils import factory
from ._types import _StateMessage
from mmlib.exceptions import (
    MatcherConnectionError,
    MatcherTimeoutError,
    MatcherProtocolError,
)

logger = logging.getLogger(__name__)


class BarefootMatcher(BaseOnlineMatcher):
    """
    Online map-matcher that communicates with Barefoot over TCP
    and receives matched states through a ZMQ PUB/SUB broker.
    """

    _matcher_name: Final[str] = "barefoot"

    def __init__(
        self,
        G_road: nx.MultiDiGraph,
        host: str = "localhost",
        port: int = 1234,
        broker_url: str = "tcp://localhost:5556",
        vehicle_id: str | None = None,
        timeout: float = 5.0,
    ):
        # Connectivity
        self._host = host
        self._port = port
        self._broker_url = broker_url
        self._vehicle_id = vehicle_id or uuid.uuid4().hex
        self._timeout = timeout

        # Graph
        self._G_road = G_road

        # ZMQ resources
        self._ctx: azmq.Context | None = None
        self._socket: azmq.Socket | None = None

        # State tracking
        self._result = OnlineMatchResult(matcher_name=self._matcher_name)
        self._pending: dict[int, GPSPoint] = {}
        self._pending_lock = asyncio.Lock()
        self._send_queue: asyncio.Queue[None] = asyncio.Queue()

    # -------------------------------------------------------------------------
    # Context manager
    # -------------------------------------------------------------------------

    @override
    async def __aenter__(self) -> Self:
        if self._ctx is not None:
            return self

        try:
            self._ctx = azmq.Context()
            self._socket = self._create_subscriber_socket(self._ctx, self._broker_url)
            logger.info("Connected to ZMQ broker at %s", self._broker_url)
        except Exception as e:
            raise MatcherConnectionError(
                f"Failed to connect to ZMQ broker at {self._broker_url}"
            ) from e

        return self

    @override
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._cleanup()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------
    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        socket = self._ensure_socket_ready()

        writer_task = asyncio.create_task(self._writer_loop(points))

        try:
            while True:
                # Wait for next expected point (or end of stream)
                if not self._send_queue.empty():
                    token = await self._send_queue.get()
                    if token is None:
                        break

                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=self._timeout)

                    data: _StateMessage = self._parse_state_message(raw)

                    coordinate = self._extract_coordinate(data)
                    if not coordinate:
                        logger.warning("Received empty matched point from Barefoot")
                        continue

                    # Attempt to correlate the matched state with original GPS
                    time_key = data.get("time")
                    async with self._pending_lock:
                        pt = self._pending.pop(time_key, None)

                    edge_id = self._snap_matched_point_to_edge(coordinate)

                    self._result._update(
                        new_point=pt,
                        matched_point=coordinate,
                        edge_id=edge_id,
                    )

                    yield self._result

                except asyncio.TimeoutError as exc:
                    logger.error("Timeout waiting for ZMQ message")
                    raise MatcherTimeoutError(
                        "Timeout waiting for ZMQ message"
                    ) from exc

        finally:
            # ensure writer is stopped
            writer_task.cancel()
            try:
                await writer_task
            except asyncio.CancelledError:
                pass

            # release all resources
            await self._cleanup()

    # -------------------------------------------------------------------------
    # ZMQ helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _create_subscriber_socket(ctx: azmq.Context, broker_url: str) -> azmq.Socket:
        socket = ctx.socket(zmq.SUB)
        socket.connect(broker_url)
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        return socket

    def _ensure_socket_ready(self) -> azmq.Socket:
        if self._socket is None:
            raise RuntimeError(
                "Matcher used outside of async context. Use 'async with matcher:'"
            )
        return self._socket

    @staticmethod
    def _parse_state_message(raw: bytes) -> _StateMessage:
        return json.loads(raw.decode())

    @staticmethod
    def _extract_coordinate(data: _StateMessage) -> Coordinate | None:
        if not data or "point" not in data:
            return None
        x, y = wkt.loads(data["point"]).coords[0]
        return Coordinate(lon=x, lat=y)

    # -------------------------------------------------------------------------
    # TCP writer
    # -------------------------------------------------------------------------

    async def _writer_loop(self, points: AsyncIterable[GPSPoint]):
        try:
            async for pt in points:
                timestamp_ms = int(pt.time.timestamp() * 1000)

                # Register BEFORE sending to avoid race condition
                async with self._pending_lock:
                    self._pending[timestamp_ms] = pt

                try:
                    reader, writer = await self._open_tcp_connection()
                except MatcherConnectionError:
                    # Remove pending entry because nothing was sent
                    async with self._pending_lock:
                        self._pending.pop(timestamp_ms, None)
                    continue

                try:
                    await self._send_point_and_wait_ack(
                        pt, timestamp_ms, reader, writer
                    )
                except Exception:
                    # Remove pending entry on failure
                    async with self._pending_lock:
                        self._pending.pop(timestamp_ms, None)
                    raise

                finally:
                    writer.close()
                    await writer.wait_closed()

        except asyncio.CancelledError:
            logger.debug("Writer loop cancelled.")
            raise

        except Exception:
            logger.exception("Critical error in writer loop")
            raise
        finally:
            # Signal end of stream
            await self._send_queue.put(None)

    async def _open_tcp_connection(self):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
            return reader, writer
        except asyncio.TimeoutError as exc:
            msg = f"Timeout connecting to Barefoot TCP at {self._host}:{self._port}"
            logger.error(msg)
            raise MatcherConnectionError(msg) from exc
        except OSError as exc:
            msg = (
                f"Failed to connect to Barefoot TCP at {self._host}:{self._port}: {exc}"
            )
            logger.error(msg)
            raise MatcherConnectionError(msg) from exc

    async def _send_point_and_wait_ack(
        self,
        pt: GPSPoint,
        timestamp_ms: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        msg = json.dumps(
            {
                "id": self._vehicle_id,
                "time": timestamp_ms,
                "point": f"POINT({pt.coordinate.lon} {pt.coordinate.lat})",
            }
        )

        writer.write(msg.encode() + b"\n")
        if writer.can_write_eof():
            writer.write_eof()

        await writer.drain()

        try:
            response = await asyncio.wait_for(reader.read(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise MatcherTimeoutError("Timeout waiting for Barefoot response") from exc

        logger.debug("Barefoot response: %s", response)

        if b"SUCCESS" not in response:
            raise MatcherProtocolError(
                f"Barefoot matching failed for point {pt}: {response.decode()}"
            )

    # -------------------------------------------------------------------------
    # Edge snapping
    # -------------------------------------------------------------------------

    def _snap_matched_point_to_edge(self, point: Coordinate) -> str:
        u, v, k = ox.nearest_edges(self._G_road, point.lon, point.lat)
        return self._G_road.edges[u, v, k]["osmid"]

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    async def _cleanup(self):
        """Release all matcher resources safely."""

        # Clear all pending entries
        async with self._pending_lock:
            self._pending.clear()

        # Close ZMQ socket
        if self._socket:
            try:
                self._socket.close(linger=0)
            except Exception:
                pass
            self._socket = None

        # Terminate context
        if self._ctx:
            try:
                self._ctx.term()
            except Exception:
                pass
            self._ctx = None

        logger.info("Barefoot matcher cleanup completed.")


@factory(BarefootMatcher)
def barefoot_matcher(*args, **kwargs):
    return BarefootMatcher(*args, **kwargs)
