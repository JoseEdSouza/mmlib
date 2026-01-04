import json
import logging
from typing import Any, override
from uuid import uuid4

import requests
from shapely import wkt

from mmlib.matcher.base import BaseMatcher
from mmlib.result.offline import MatchResult
from mmlib.types.points import Coordinate, GPSPoint
from mmlib.utils import factory

logger = logging.getLogger(__name__)


class GraphiumOfflineMatcher(BaseMatcher):
    """
    Offline matcher using the Graphium API.
    """

    def __init__(
        self,
        base_url: str,
        graph_name: str,
        version: str = "current",
        timeout_s: float = 60.0,
    ) -> None:
        super().__init__()
        self._base_url = base_url
        self._graph_name = graph_name
        self._version = version
        self._id = hash(uuid4().hex)

        if timeout_s <= 0:
            raise ValueError("Timeout must be a positive value.")

        self._timeout_ms = int(timeout_s * 1000)
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._params = {
            "outputVerbose": "false",
            "timeoutMs": str(self._timeout_ms),
        }
        self._url = f"{self._base_url}/matching/graphs/{self._graph_name}/versions/{self._version}/matchtrack"

    @property
    def matcher_name(self) -> str:
        """The name of the matcher."""
        return "GraphiumOfflineMatcher"

    @override
    def match(self, points: list[GPSPoint]) -> MatchResult:
        """
        Map match the provided GPS points using Graphium.
        """
        response = self._request(points)
        return MatchResult(
            matcher_name=self.matcher_name,
            measurement_points=points,
            matched_points=response["points"],
            edge_ids=response["edge_ids"],
        )

    def match_with_extra_params(
        self,
        points: list[GPSPoint],
        extra_params: dict[str, Any] | None = None,
        session: requests.Session | None = None,
    ) -> tuple[MatchResult, str | None, list[dict]]:
        """
        Map match com parâmetros extras (ex: startSegmentId).
        
        Returns:
            tuple: (MatchResult, last_segment_id, parsed_segments)
        """
        response = self._request(points, extra_params, session=session)
        
        match_result = MatchResult(
            matcher_name=self.matcher_name,
            measurement_points=points,
            matched_points=response["points"],
            edge_ids=response["edge_ids"],
        )
        
        # Retorna a trinca solicitada
        return (
            match_result, 
            response["last_segment_id"], 
            response["parsed_segments"]
        )

    def _request(
        self,
        points: list[GPSPoint],
        extra_params: dict[str, Any] | None = None,
        session: requests.Session | None = None,
    ) -> dict:
        """
        Envia o request e processa a resposta criando tanto a lista achatada
        quanto a lista estruturada (parsed_segments).
        """

        track_points = [
            {
                "id": i,
                "timestamp": int(ts.timestamp() * 1000),
                "x": lon,
                "y": lat,
                "z": 0,
            }
            for i, (lat, lon, ts) in enumerate(points)
        ]

        payload = {"id": self._id, "trackPoints": track_points}
        params = self._params | (extra_params or {})
        post = session.post if session else requests.post

        try:
            response = post(self._url, params=params, json=payload, headers=self._headers)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Request falhou: {e}")
            raise

        if not response.content:
            return {
                "points": [],
                "edge_ids": [],
                "last_segment_id": None,
                "parsed_segments": []
            }

        res = json.loads(response.content)
        segments = res.get("segments", [])

        # Estruturas de retorno
        all_coordinates = []
        all_edge_ids = []
        parsed_segments = []

        for seg in segments:
            # 1. Processar Geometria
            geom_wkt = seg.get("geometry")
            coords = []
            if geom_wkt:
                geom = wkt.loads(geom_wkt)
                # Shapely (lon, lat) -> Coordinate (lat, lon)
                coords = [Coordinate(lat, lon) for lon, lat in geom.coords]
            
            # 2. Identificadores
            # segmentId é o ID interno do Graphium (usado para continuidade/topologia)
            seg_id = str(seg.get("segmentId")) 
            # wayId é o ID do OSM (usado para visualização/resultado final)
            way_id = str(seg.get("wayId"))

            # 3. Montar objeto estruturado para deduplicação no OnlineMatcher
            parsed_segments.append({
                "id": seg_id,       # Importante: usar segmentId para a lógica de 'startSegmentId'
                "coords": coords,
                "way_id": way_id
            })

            # 4. Montar listas achatadas (flattened) para o MatchResult padrão
            all_coordinates.extend(coords)
            all_edge_ids.append(way_id)

        last_segment_id = segments[-1]["segmentId"] if segments else None

        return {
            "points": all_coordinates,
            "edge_ids": all_edge_ids,
            "last_segment_id": last_segment_id,
            "parsed_segments": parsed_segments
        }

@factory(GraphiumOfflineMatcher)
def graphium_offline_matcher(*args, **kwargs) -> GraphiumOfflineMatcher:
    return GraphiumOfflineMatcher(*args, **kwargs)