import json
import logging
from datetime import datetime
from pathlib import Path
from mmlib.matcher.offline.barefoot import BarefootOfflineMatcher
from mmlib.types import GPSPoint

logging.basicConfig(level=logging.DEBUG)


PAYLOAD_PATH = Path(__file__).parent / "data" / "payload-matcher.json"


def test_barefoot_offline():
    # Mock points
    raw_points = json.loads(PAYLOAD_PATH.read_text())
    parsed_points = []
    for item in raw_points:
        wkt = item["point"]
        coords_str = wkt.replace("POINT", "").replace("(", "").replace(")", "").strip()
        lon_str, lat_str = coords_str.split()
        lat = float(lat_str)
        lon = float(lon_str)
        pt_time = datetime.fromisoformat(item["time"])
        parsed_points.append(GPSPoint(lat=lat, lon=lon, time=pt_time))

    print(parsed_points[0].time)
    matcher = BarefootOfflineMatcher(host="localhost", port=1234)

    print(
        "Iniciando match offline (espera-se falha se o servidor não estiver rodando)..."
    )
    try:
        result = matcher.match(parsed_points)
        print("Resultado do Match:")
        print(f"Matcher: {result.matcher_name}")
        print(f"Edge IDs: {result.edge_ids}")
        print(f"Matched Points: {len(result.matched_points)}")

        if not result.matched_points:
            print("AVISO: Nenhum matched point retornado!")
        else:
            print(f"Primeiro matched point: {result.matched_points[0]}")

    except Exception as e:
        print(f"Erro esperado ao testar sem servidor: {e}")


if __name__ == "__main__":
    test_barefoot_offline()
