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

    Pipeline:
        - _writer_loop sends GPS points to Barefoot via TCP.
        - For each acknowledged point ("SUCCESS"), the point is pushed into _send_queue.
        - match_stream consumes _send_queue, receives state updates from ZMQ,
          snaps the matched coordinate to the nearest street edge and updates OnlineMatchResult.
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
        # Connectivity configuration
        self._host: str = host
        self._port: int = port
        self._broker_url: str = broker_url
        self._vehicle_id: str = vehicle_id or uuid.uuid4().hex
        self._timeout: float = timeout

        # Street-network graph
        self._G_road: nx.MultiDiGraph = G_road

        # Resources managed by async context
        self._ctx: azmq.Context | None = None
        self._socket: azmq.Socket | None = None

        # Queue synchronizing the TCP writer and ZMQ reader
        self._send_queue: asyncio.Queue[GPSPoint | None] = asyncio.Queue()

        # Accumulated match result stream
        self._result: OnlineMatchResult = OnlineMatchResult(
            matcher_name=self._matcher_name
        )

    # -------------------------------------------------------------------------
    # Context manager
    # -------------------------------------------------------------------------

    @override
    async def __aenter__(self) -> Self:
        """
        Initializes ZMQ context and subscriber socket.
        """
        if self._ctx is not None:
            # Already initialized (idempotent)
            return self

        try:
            self._ctx = azmq.Context()
            self._socket = self._create_subscriber_socket(self._ctx, self._broker_url)
            logger.info("Connected to ZMQ broker at %s", self._broker_url)
        except Exception as e:  # noqa: BLE001
            raise MatcherConnectionError(
                f"Failed to connect to ZMQ broker at {self._broker_url}"
            ) from e

        return self

    @override
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        Cleans up ZMQ resources.
        """
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

        if self._ctx is not None:
            try:
                self._ctx.term()
            finally:
                self._ctx = None

        logger.info("Disconnected from ZMQ broker")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        """
        Consumes an async iterable of GPSPoint and yields an OnlineMatchResult
        each time the Barefoot server emits a matched state via ZMQ.

        Must be used within an async context manager:

            async with BarefootMatcher(...) as matcher:
                async for result in matcher.match_stream(points):
                    ...
        """
        socket = self._ensure_socket_ready()

        # Writer task: sends GPS points via TCP and populates _send_queue
        writer_task = asyncio.create_task(self._writer_loop(points))

        try:
            while True:
                pt = await self._send_queue.get()

                # End-of-stream sentinel
                if pt is None:
                    break

                try:
                    raw = await asyncio.wait_for(
                        socket.recv(), timeout=self._timeout
                    )

                    data: _StateMessage = self._parse_state_message(raw)
                    coordinate = self._extract_coordinate(data)
                    edge_id = self._snap_matched_point_to_edge(coordinate)

                    # Update accumulated result
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
                except Exception as exc:  # noqa: BLE001
                    logger.error("Error processing ZMQ message: %s", exc, exc_info=True)

        finally:
            # Ensure background writer is canceled if iteration stops early
            if not writer_task.done():
                writer_task.cancel()
                try:
                    await writer_task
                except asyncio.CancelledError:
                    pass

    # -------------------------------------------------------------------------
    # ZMQ helpers
    # -------------------------------------------------------------------------


    @staticmethod
    def _create_subscriber_socket(
        ctx: azmq.Context,
        broker_url: str,
    ) -> azmq.Socket:
        """
        Creates and configures a SUB socket for the ZMQ broker.
        """
        socket = ctx.socket(zmq.SUB)
        socket.connect(broker_url)
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        return socket

    def _ensure_socket_ready(self) -> azmq.Socket:
        """
        Ensures the SUB socket is initialized; raises if matcher is used outside an async context.
        """
        if self._socket is None:
            raise RuntimeError(
                "Matcher used outside of async context. Use 'async with matcher:'"
            )
        return self._socket

    @staticmethod
    def _parse_state_message(raw: bytes) -> _StateMessage:
        """
        Parses a raw ZMQ frame into a typed state message.
        """
        data: _StateMessage = json.loads(raw.decode())
        return data

    @staticmethod
    def _extract_coordinate(data: _StateMessage) -> Coordinate:
        """
        Converts a WKT point ("POINT(lon lat)") into a Coordinate object.
        """
        if len(data) == 0 or "point" not in data:
            print(data)
            raise ValueError("Invalid state message format")
        x, y = wkt.loads(data["point"]).coords[0]
        return Coordinate(lon=x, lat=y)

    # -------------------------------------------------------------------------
    # TCP writer helpers
    # -------------------------------------------------------------------------

    async def _writer_loop(self, points: AsyncIterable[GPSPoint]) -> None:
        """
        Background task that sends GPS points to the Barefoot TCP server.

        For each successfully acknowledged point ("SUCCESS"), the point is added
        to _send_queue so match_stream can process it alongside ZMQ messages.

        Errors for individual points do not break the loop.
        """
        try:
            async for pt in points:
                try:
                    reader, writer = await self._open_tcp_connection()
                except MatcherConnectionError:
                    # Already logged inside _open_tcp_connection
                    continue

                try:
                    await self._send_point_and_wait_ack(pt, reader, writer)
                except MatcherProtocolError as exc:
                    logger.error("%s", exc)
                except MatcherTimeoutError as exc:
                    logger.error("%s", exc)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Unexpected error in writer loop: %s", exc, exc_info=True)
                finally:
                    writer.close()
                    await writer.wait_closed()

                # Notify consumer that this point was processed
                await self._send_queue.put(pt)

        except Exception:  # noqa: BLE001
            logger.exception("Critical error in writer loop")
            raise
        finally:
            # End-of-stream sentinel
            await self._send_queue.put(None)

    async def _open_tcp_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """
        Opens a TCP connection to the Barefoot server with timeout handling.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
            return reader, writer
        except asyncio.TimeoutError as exc:
            msg = f"Timeout connecting to {self._host}:{self._port}"
            logger.error(msg)
            raise MatcherConnectionError(msg) from exc
        except OSError as exc:
            msg = f"Failed to connect to Barefoot TCP at {self._host}:{self._port}: {exc}"
            logger.error(msg)
            raise MatcherConnectionError(msg) from exc

    async def _send_point_and_wait_ack(
        self,
        pt: GPSPoint,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Serializes a GPSPoint to JSON, sends it to the Barefoot server,
        and waits for an ACK containing the substring "SUCCESS".

        Raises:
            MatcherTimeoutError  – if ACK does not arrive in time.
            MatcherProtocolError – if response does not contain "SUCCESS".
        """
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
        """
        Finds the nearest street edge in the OSMnx graph and returns its osmid.
        """
        u, v, k = ox.nearest_edges(self._G_road, point.lon, point.lat)
        return self._G_road.edges[u, v, k]["osmid"]


@factory(BarefootMatcher)
def barefoot_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return BarefootMatcher(*args, **kwargs)
