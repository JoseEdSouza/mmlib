import asyncio
import json
from typing import Any, AsyncIterable, Mapping, override
import uuid

import zmq
import zmq.asyncio as azmq

from mmlib.matcher.base import BaseOnlineMatcher
from mmlib.result import OnlineMatchResult
from mmlib.types import Coordinate, GPSPoint


class Matcher(BaseOnlineMatcher):
    _matcher_name = "barefoot"

    _port: int
    _host: str
    _broker_url: str

    _vehicle_id: str

    _reader: asyncio.StreamReader | None
    _writer: asyncio.StreamWriter | None

    _context: azmq.Context | None = None
    _sub: azmq.Socket | None = None

    def __init__(
        self, host: str, port: int, broker_url: str, vehicle_id: str | None = None
    ):
        self._host = host
        self._port = port

        self._broker_url = broker_url

        self._vehicle_id = vehicle_id or uuid.uuid4().hex

        # Inicializa conexões e resultado tipados
        self._reader = None
        self._writer = None
        self._context = None
        self._sub = None

        self._result: OnlineMatchResult = OnlineMatchResult(
            matcher_name=self._matcher_name
        )

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, azmq.Socket]:
        """Abre conexão TCP e ZMQ somente quando match() for chamado."""
        if self._reader is None or self._writer is None:
            self._reader, self._writer = await asyncio.open_connection(
                self._host, self._port
            )

        if self._context is None:
            self._context = azmq.Context()
        if self._sub is None:
            self._sub = self._context.socket(zmq.SUB)
            self._sub.connect(f"tcp://{self._host}:{self._port + 1}")
            self._sub.setsockopt_string(zmq.SUBSCRIBE, "")

        # Garantimos para o type-checker que nada é None neste retorno
        return self._reader, self._writer, self._sub

    async def _match(self, points: AsyncIterable[GPSPoint]) -> OnlineMatchResult:
        _, writer, sub = await self._connect()

        # executa leitura ZMQ e escrita TCP em paralelo
        writer_task = asyncio.create_task(self.write_job(writer, points))
        reader_task = asyncio.create_task(self.read_job(sub))

        await writer_task  # espera enviar tudo
        reader_task.cancel()  # opcional: para de ler quando termina

        return self._result

    @override
    def match(self, points: AsyncIterable[GPSPoint]) -> OnlineMatchResult:
        loop = asyncio.get_event_loop()
        loop.create_task(self._match(points))
        return self._result

    async def write_job(
        self, writer: asyncio.StreamWriter, points: AsyncIterable[GPSPoint]
    ) -> None:
        async for pt in points:
            msg = json.dumps(
                {
                    "id": self._vehicle_id,  # ajuste ao seu modelo
                    "time": int(pt.time.timestamp() * 1000),
                    "point": f"POINT ({pt.coordinate.lon} {pt.coordinate.lat})",
                }
            )
            writer.write(msg.encode() + b"\n")
            await writer.drain()

        writer.write_eof()

    @staticmethod
    def _to_coordinate(raw: Any) -> Coordinate | None:
        """Best-effort conversion of different payload shapes into a Coordinate."""
        if isinstance(raw, Coordinate):
            return raw
        if isinstance(raw, GPSPoint):
            return raw.coordinate
        if isinstance(raw, str):
            text = raw.strip()
            if text.upper().startswith("POINT"):
                try:
                    lon_str, lat_str = text.split("(", 1)[1].rstrip(")").split()[:2]
                    return Coordinate(float(lat_str), float(lon_str))
                except (ValueError, IndexError):
                    return None
        if isinstance(raw, Mapping):
            lat = raw.get("lat") or raw.get("latitude")
            lon = raw.get("lon") or raw.get("longitude")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                return Coordinate(float(lat), float(lon))
        return None

    async def read_job(self, sub: azmq.Socket) -> None:
        """Recebe as mensagens enviadas pelo Barefoot (map-matching results)."""
        while True:
            try:
                raw = await sub.recv()
            except asyncio.CancelledError:
                break

            data: dict[str, Any] = json.loads(raw.decode())
            print("Resultado do Barefoot:", data)

            measurement = self._to_coordinate(
                data.get("measurement") or data.get("point") or data.get("gps")
            )
            matched_point = self._to_coordinate(
                data.get("matched_point")
                or data.get("matchedPoint")
                or data.get("matched")
            )

            edge_raw = data.get("edge_id") or data.get("edgeId") or data.get("edge")
            edge_id = str(edge_raw) if edge_raw is not None else None

            if measurement is None and matched_point is not None:
                # Fallback: some payloads only send matched point.
                measurement = matched_point

            if measurement is None and matched_point is None and edge_id is None:
                # Nada para atualizar
                continue

            self._result._update(
                measurement, matched_point=matched_point, edge_id=edge_id
            )
