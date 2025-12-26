import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from typing import Final, Self, override

from shapely import wkt

from mmlib.matcher.base import BaseOnlineMatcher
from mmlib.matcher.online.barefoot._communicator import _BarefootCommunicator
from mmlib.matcher.online.barefoot._synchronizer import _Synchronizer
from mmlib.matcher.online.barefoot._types import _PointMessage, _StateMessage
from mmlib.result import OnlineMatchResult
from mmlib.types import Coordinate, GPSPoint
from mmlib.utils import factory

logger = logging.getLogger(__name__)


class BarefootMatcher(BaseOnlineMatcher):
    """Online matcher using Barefoot (TCP publish + ZMQ subscribe).

    Notes:
    - There is a 1:1 relationship between sent points and received states.
    - When the input stream ends, we keep draining states until all sent
      points are received or we observe `drain_timeout` seconds of silence.
    """

    _matcher_name: Final[str] = "barefoot"

    def __init__(
        self,
        pub_host: str = "localhost",
        pub_port: int = 1234,
        sub_host: str = "localhost",
        sub_port: int | str = 5556,
        *,
        vehicle_id: str | None = None,
        drain_timeout: float = 5.0,
    ) -> None:
        if vehicle_id is None:
            import uuid

            vehicle_id = str(uuid.uuid4())

        self._vehicle_id = vehicle_id
        self._drain_timeout = drain_timeout
        self._comm = _BarefootCommunicator(pub_host, pub_port, sub_host, sub_port)
        self._result = OnlineMatchResult(matcher_name=self._matcher_name)
        self._sync = _Synchronizer(max_inflight=1)

    @override
    async def __aenter__(self) -> Self:
        await self._comm.__aenter__()
        return self

    @override
    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._comm.__aexit__(exc_type, exc, tb)

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        sending_done = asyncio.Event()

        sender_task = asyncio.create_task(
            self._sender(points, sending_done), name="barefoot_sender"
        )

        try:
            async for res in self._receiver(sending_done, sender_task):
                yield res

        finally:
            sender_task.cancel()
            await asyncio.gather(sender_task, return_exceptions=True)

    async def _sender(
        self, points: AsyncIterable[GPSPoint], done: asyncio.Event
    ) -> None:
        try:
            async for pt in points:
                # Wait for the previous point to be processed (strict 1:1)
                await self._sync.wait_to_send()

                msg: _PointMessage = {
                    "id": self._vehicle_id,
                    "time": int(pt.time.timestamp() * 1000),
                    "point": f"POINT({pt.coordinate.lon} {pt.coordinate.lat})",
                }
                try:
                    await asyncio.wait_for(self._comm.submit_point(msg), timeout=3.0)
                    await self._sync.notify_sent()
                    self._result._update_sent(pt)
                except asyncio.TimeoutError:
                    logger.warning("Timeout sending point to Barefoot; continuing.")
        finally:
            done.set()

    async def _receiver(
        self,
        sending_done: asyncio.Event,
        sender_task: asyncio.Task[None],
    ) -> AsyncIterator[OnlineMatchResult]:
        state_iter = aiter(self._comm.messages())

        while True:
            # Check if we should stop
            if await self._sync.is_finished(sending_done.is_set() or sender_task.done()):
                return

            try:
                # Use drain_timeout as a safety buffer
                state = await asyncio.wait_for(
                    anext(state_iter),
                    timeout=self._drain_timeout,
                )
            except asyncio.TimeoutError:
                if await self._sync.is_finished(
                    sending_done.is_set() or sender_task.done()
                ):
                    logger.debug(
                        "Timeout waiting for barefoot states (sent: %d, received: %d). Closing.",
                        self._sync.sent_count,
                        self._sync.received_count,
                    )
                    return
                else:
                    continue
            except StopAsyncIteration:
                return

            # Mark as processed and notify sender
            await self._sync.notify_received()

            coord = self._state_to_coordinate(state)
            if coord is None:
                continue

            self._result._update_matched(
                matched_point=coord,
                edge_id=self._state_to_edge_id(state),
            )
            yield self._result

    @staticmethod
    def _state_to_coordinate(data: _StateMessage) -> Coordinate | None:
        pt_wkt = data.get("point")
        if not pt_wkt:
            return None
        x, y = wkt.loads(pt_wkt).coords[0]
        return Coordinate(lon=x, lat=y)

    @staticmethod
    def _state_to_edge_id(data: _StateMessage) -> str | None:
        edge_gid = data.get("osm_id")
        return str(edge_gid) if edge_gid is not None else None


@factory(BarefootMatcher)
def barefoot_matcher(*args, **kwargs):
    return BarefootMatcher(*args, **kwargs)
