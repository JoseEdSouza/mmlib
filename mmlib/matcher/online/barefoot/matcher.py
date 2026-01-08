import asyncio
import copy
import logging
from collections.abc import AsyncIterable, AsyncIterator
from typing import Final, override

from shapely import wkt

from mmlib.matcher.base import BaseOnlineMatcher
from mmlib.matcher.online.barefoot._communicator import _BarefootCommunicator
from mmlib.matcher.online.barefoot._synchronizer import _Synchronizer
from mmlib.matcher.online.barefoot._types import _PointMessage, _StateMessage
from mmlib.result import OnlineMatchResult
from mmlib.types import Coordinate, GPSPoint
from mmlib.utils import factory

logger = logging.getLogger(__name__)


class BarefootOnlineMatcher(BaseOnlineMatcher):
    """Online matcher using Barefoot (TCP publish + ZMQ subscribe).

    Notes:
    - Strict 1:1 send/receive (max_inflight=1).
    - After input ends, we drain until all sent are received, or `drain_timeout` silence.
    - Defensive correlation via (id, time) to avoid mixing vehicles/out-of-order/duplicates.
    """

    _matcher_name: Final[str] = "barefoot_online"

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
        super().__init__()
        if vehicle_id is None:
            import uuid

            vehicle_id = str(uuid.uuid4())

        self._vehicle_id = vehicle_id
        self._drain_timeout = drain_timeout
        self._comm = _BarefootCommunicator(pub_host, pub_port, sub_host, sub_port)

        self._result = OnlineMatchResult(matcher_name=self._matcher_name)
        self._sync = _Synchronizer(max_inflight=1)

        # Correlation guards
        self._last_sent_time_ms: float | None = None
        self._last_received_time_ms: int | None = None

    @property
    def matcher_name(self) -> str:
        return self._matcher_name

    @override
    async def start(self) -> None:
        if not self._started:
            await self._comm.__aenter__()
            self._started = True

        # Reset state for reusability
        self._result = OnlineMatchResult(matcher_name=self._matcher_name)
        self._sync = _Synchronizer(max_inflight=1)
        self._last_sent_time_ms = None
        self._last_received_time_ms = None

    @override
    async def stop(self) -> None:
        if self._started:
            await self._comm.__aexit__(None, None, None)
            self._started = False

        # Clear state
        self._last_sent_time_ms = None
        self._last_received_time_ms = None

    @override
    async def match_stream(
        self, points: AsyncIterable[GPSPoint]
    ) -> AsyncIterator[OnlineMatchResult]:
        await self.start()
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

    # ------------------------ helpers (sender) ------------------------

    def _make_point_message(self, pt: GPSPoint) -> _PointMessage:
        return {
            "id": self._vehicle_id,
            "time": pt.time.timestamp() * 1000,
            "point": f"POINT({pt.coordinate.lon} {pt.coordinate.lat})",
        }

    async def _submit_point_with_timeout(
        self, msg: _PointMessage, timeout_s: float
    ) -> bool:
        try:
            await asyncio.wait_for(self._comm.submit_point(msg), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout sending point to Barefoot; dropping and continuing."
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Unexpected error sending point to Barefoot; dropping and continuing."
            )
            return False

    # ------------------------ helpers (receiver) ------------------------

    def _should_accept_state(self, state: _StateMessage) -> bool:
        # Vehicle filter
        if state.get("id") != self._vehicle_id:
            return False

        st_time = state.get("time")
        if st_time is None:
            return False

        # Drop duplicates / out-of-order states
        if (
            self._last_received_time_ms is not None
            and st_time <= self._last_received_time_ms
        ):
            return False

        # Safety: ignore states that go "backwards" relative to last successfully sent
        if self._last_sent_time_ms is not None and st_time < self._last_sent_time_ms:
            return False

        # Must have a parseable point
        if self._state_to_coordinate(state) is None:
            return False

        return True

    def _update_result_from_state(self, state: _StateMessage) -> None:
        coord = self._state_to_coordinate(state)
        if coord is None:
            return
        self._result._update_matched(
            matched_point=coord,
            edge_id=None,
        )
        if (path := state.get("path_osm_ids")) is not None and len(path) > 0:
            self._result.edge_ids = [str(osm_id) for osm_id in path]

    def _snapshot_result(self) -> OnlineMatchResult:
        # Snapshot to avoid yielding the same mutable object repeatedly
        try:
            return copy.deepcopy(self._result)
        except Exception as e:
            logger.warning("Error deepcopying result: %s. Returning mutable result.", e)
            return self._result

    # ------------------------ pipeline ------------------------

    async def _sender(
        self, points: AsyncIterable[GPSPoint], done: asyncio.Event
    ) -> None:
        try:
            async for pt in points:
                await self._sync.wait_to_send()

                msg = self._make_point_message(pt)
                ok = await self._submit_point_with_timeout(msg, timeout_s=3.0)
                if not ok:
                    continue

                await self._sync.notify_sent()
                self._last_sent_time_ms = msg["time"]
                self._result._update_sent(pt)
        finally:
            done.set()

    async def _receiver(
        self,
        sending_done: asyncio.Event,
        sender_task: asyncio.Task[None],
    ) -> AsyncIterator[OnlineMatchResult]:
        state_iter = aiter(self._comm.messages())

        while True:
            if await self._sync.is_finished(
                sending_done.is_set() or sender_task.done()
            ):
                return

            try:
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
                continue
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "Unexpected error receiving state from Barefoot; continuing."
                )
                continue

            # Validate/correlate BEFORE releasing sender
            try:
                if not self._should_accept_state(state):
                    continue
            except Exception:
                logger.exception("Error validating Barefoot state; ignoring.")
                continue

            self._last_received_time_ms = int(state["time"])
            await self._sync.notify_received()

            self._update_result_from_state(state)
            yield self._snapshot_result()

    # ------------------------ WKT helpers ------------------------

    @staticmethod
    def _state_to_coordinate(data: _StateMessage) -> Coordinate | None:
        pt_wkt = data.get("point")
        if not pt_wkt:
            return None
        try:
            geom = wkt.loads(pt_wkt)
            x, y = geom.coords[0]
            return Coordinate(lon=x, lat=y)
        except Exception:
            logger.warning("Invalid WKT point from Barefoot: %r", pt_wkt)
            return None

    @staticmethod
    def _state_to_edge_id(data: _StateMessage) -> str | None:
        edge_gid = data.get("osm_id")
        return str(edge_gid) if edge_gid is not None else None


@factory(BarefootOnlineMatcher)
def barefoot_online_matcher(*args, **kwargs) -> BaseOnlineMatcher:
    return BarefootOnlineMatcher(*args, **kwargs)
