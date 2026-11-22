"""
Pasarela de voz: Asterisk (AudioSocket) <-> faster-whisper <-> API del agente <-> Piper.

Correr desde E:\\vertex-voice-agenda-agent\\telephony :
    .\\venv\\Scripts\\python.exe -m gateway
"""
import asyncio
import logging

from .agent_client import AgentClient
from .call_session import CallSession
from .config import load_settings
from .speech import PiperTTS, WhisperSTT

log = logging.getLogger("gateway")


async def serve(settings, stt, tts) -> None:
    agent = AgentClient(settings.api_url, settings.model_name)

    async def on_connect(reader, writer):
        peer = writer.get_extra_info("peername")
        log.info("Conexión AudioSocket desde %s", peer)
        try:
            await CallSession(reader, writer, settings=settings, stt=stt, tts=tts, agent=agent).run()
        except Exception:  # una llamada rota no tumba la pasarela
            log.exception("Error en la llamada")
            writer.close()

    async def latido() -> None:
        """Le dice a la API cada 4 s que la pasarela está viva (la UI lo muestra en el estado de la línea)."""
        while True:
            await agent.heartbeat()
            await asyncio.sleep(4)

    latidos = asyncio.create_task(latido())
    server = await asyncio.start_server(on_connect, settings.gateway_host, settings.gateway_port)
    log.info("Pasarela escuchando en %s:%s (API del agente: %s)", settings.gateway_host, settings.gateway_port,
             settings.api_url)
    try:
        async with server:
            await server.serve_forever()
    finally:
        latidos.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # sin una línea por cada latido y cada reporte
    settings = load_settings()
    tts = PiperTTS(settings.piper_voice)        # primero la voz: si falta el archivo, falla rápido
    stt = WhisperSTT(settings.whisper_model, settings.whisper_dir)
    log.info("Prueba de voz: %d ms de audio sintetizado.", len(tts.synthesize("Listo.")) // 16)
    try:
        asyncio.run(serve(settings, stt, tts))
    except KeyboardInterrupt:
        log.info("Pasarela detenida.")


if __name__ == "__main__":
    main()
