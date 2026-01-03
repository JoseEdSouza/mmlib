import asyncio
from contextlib import contextmanager
import socket
import json
import logging
from queue import Queue
import time
import threading
from typing import AsyncGenerator, AsyncIterator, Self, cast

import zmq
import zmq.asyncio as azmq

from mmlib.matcher.online.barefoot._types import _PointMessage, _StateMessage


logger = logging.getLogger(__name__)


@contextmanager
def _open_socket(host: str, port: int):
    sock = socket.create_connection((host, port), timeout=3.0)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        yield sock
    finally:
        sock.shutdown(socket.SHUT_WR)
        sock.close()


def _pub_point_sync(host: str, port: int, q: Queue[_PointMessage | None]) -> None:
    backoff = 0.2

    while True:
        pt = q.get()
        if pt is None:
            return

        payload = (json.dumps(pt) + "\n").encode("utf-8")

        while True:
            try:
                with _open_socket(host, port) as s:
                    s.sendall(payload)
                    logger.debug("Sent point to Barefoot (%s:%s): %s", host, port, pt)
                backoff = 0.2
                break

            except OSError as e:
                logger.warning(
                    "Error when sending to Barefoot (%s:%s): %s", host, port, e
                )

                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)


class _BarefootPointPublisher:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = int(port)
        self._q: Queue[_PointMessage | None] = Queue()
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        logger.debug(
            "Starting Barefoot point publisher to %s:%s", self._host, self._port
        )
        if self._thread is None:
            self._thread = threading.Thread(
                target=_pub_point_sync,
                args=(self._host, self._port, self._q),
                name="barefoot_publisher",
                daemon=True,
            )
            self._thread.start()

    async def stop(self) -> None:
        logger.debug(
            "Stopping Barefoot point publisher to %s:%s", self._host, self._port
        )
        # Sinaliza fim
        self._q.put(None)
        if self._thread is not None:
            # join can block; run in executor to avoid blocking the event loop
            await asyncio.to_thread(self._thread.join, 5.0)
            self._thread = None

    async def __aenter__(self) -> "_BarefootPointPublisher":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def submit_point(self, point: _PointMessage) -> None:
        if self._thread is None:
            raise RuntimeError(
                "Publisher not started. Call 'start' before submitting points."
            )
        # put() is sync and can block if you set maxsize; so send via to_thread
        await asyncio.to_thread(self._q.put, point)


async def _read_socket(socket: azmq.Socket) -> AsyncGenerator[_StateMessage, None]:
    while True:
        try:
            raw = await socket.recv()
        except asyncio.CancelledError:
            return
        except zmq.error.ZMQError:
            return

        try:
            decoded = raw.decode("utf-8")
            json_msg = json.loads(decoded)
        except Exception as e:
            logger.warning("Error decoding Barefoot message: %s", e)
            continue

        if not isinstance(json_msg, dict):
            continue
        yield cast(_StateMessage, json_msg)


class _BarefootSubscriber:
    def __init__(self, host: str, port: int | str) -> None:
        self._host = host
        self._port = str(port)
        self._context = azmq.Context()
        self._socket: azmq.Socket | None = None

    async def start(self) -> None:
        logger.debug("Starting Barefoot subscriber to %s:%s", self._host, self._port)
        if self._socket is None:
            self._socket = self._context.socket(zmq.SUB)
            self._socket.connect(f"tcp://{self._host}:{self._port}")
            self._socket.setsockopt_string(zmq.SUBSCRIBE, "")

    async def stop(self) -> None:
        logger.debug("Stopping Barefoot subscriber to %s:%s", self._host, self._port)
        if self._socket:
            self._socket.close()
            self._socket = None

    async def __aenter__(self) -> "_BarefootSubscriber":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def messages(self) -> AsyncIterator[_StateMessage]:
        if self._socket is None:
            raise RuntimeError(
                "Subscriber not started. Call 'start' before reading messages."
            )
        async for msg in _read_socket(self._socket):
            yield msg


class _BarefootCommunicator:
    def __init__(
        self, pub_host: str, pub_port: int, sub_host: str, sub_port: int | str
    ) -> None:
        self._publisher = _BarefootPointPublisher(pub_host, pub_port)
        self._subscriber = _BarefootSubscriber(sub_host, sub_port)

    async def __aenter__(self) -> Self:
        await self._publisher.__aenter__()
        await self._subscriber.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._publisher.__aexit__(exc_type, exc, tb)
        await self._subscriber.__aexit__(exc_type, exc, tb)

    async def submit_point(self, point: _PointMessage) -> None:
        await self._publisher.submit_point(point)

    async def messages(self) -> AsyncIterator[_StateMessage]:
        async for msg in self._subscriber.messages():
            yield msg
