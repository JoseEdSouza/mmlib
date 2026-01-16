import asyncio
import json
from datetime import datetime
import logging
from pathlib import Path
from mmlib.matcher import BarefootOnlineMatcher
from mmlib.types import GPSPoint

# Configuração do servidor Barefoot
PUB_HOST = "localhost"
PUB_PORT = 1235
SUB_HOST = "localhost"
SUB_PORT = 1236
VEHICLE_ID = None  # Será gerado ou atualizado

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

PAYLOAD_PATH = Path(__file__).parent / "data" / "payload-tracker.json"


async def main():
    # 1. Carrega dados
    try:
        data = json.loads(PAYLOAD_PATH.read_text())
    except FileNotFoundError:
        print("Erro: Arquivo payload-tracker.json não encontrado.")
        return

    points = []
    print(f"Carregando {len(data)} pontos do arquivo...")

    for item in data:
        wkt = item["point"]
        try:
            coords_str = (
                wkt.replace("POINT", "").replace("(", "").replace(")", "").strip()
            )
            lon_str, lat_str = coords_str.split()
            lat = float(lat_str)
            lon = float(lon_str)
            pt_time = datetime.fromtimestamp(item["time"] / 1000)
            points.append(GPSPoint(lat=lat, lon=lon, time=pt_time))
        except ValueError:
            continue

    if not points:
        print("Nenhum ponto válido carregado.")
        return

    print(f"Carregados {len(points)} pontos de teste.")

    # 2. Gerador de pontos
    async def point_generator():
        for p in points:
            await asyncio.sleep(0.5)  # Simula chegada em tempo real
            yield p

    # 3. Uso do Matcher com a nova API (Context Manager)
    print("\nIniciando envio de pontos para o Barefoot (Modo Stream)...")

    print("Inicializando Matcher...")

    print("Iniciando processo de map matching...")

    try:
        async with BarefootOnlineMatcher(
            pub_host=PUB_HOST,
            pub_port=PUB_PORT,
            sub_host=SUB_HOST,
            sub_port=SUB_PORT,
            vehicle_id=VEHICLE_ID,
        ) as matcher:
            print("Conectado ao Barefoot.")
            i = 0
            async for result in matcher.match_stream(point_generator()):
                i += 1
                edge_count = len(result.edge_ids) if result.edge_ids else 0
                print(f"[{i}] Update recebido. Edges no caminho: {edge_count}")
                # print(f"Current Result State: {result}")

        print("\nProcesso finalizado com sucesso.")

        async with BarefootOnlineMatcher(
            pub_host=PUB_HOST,
            pub_port=PUB_PORT,
            sub_host=SUB_HOST,
            sub_port=SUB_PORT,
            vehicle_id=VEHICLE_ID,
            drain_timeout=10000,
        ) as matcher:
            print(
                "\n--- Testando match_batch (Uso Offline-like para Online Matcher) ---"
            )
            try:
                # Nota: Matcher atualmente implementa match_stream e é um wrapper.
                # BaseOnlineMatcher.match_batch deve lidar com o context manager automaticamente.
                result = await matcher.match_batch(points)
                print(f"Batch Result Edge Count: {len(result.edge_ids)}")
                print(f"Matched Points: {len(result.matched_points)}")

                # Teste de exportação genérica
                print("DataFrame Head:")
                print(result.to_df().head())

                print("\nGeoJSON Properties:")
                print(result.to_geojson()["properties"])

            except Exception as e:
                print(f"Erro: {e}")
                import traceback

                traceback.print_exc()

    except Exception as e:
        print(f"Ocorreu um erro durante a execução: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
