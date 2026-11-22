"""
Pruebas de la pasarela sin Asterisk, sin modelos y sin la API: un "Asterisk" falso habla AudioSocket por TCP real.

Correr desde E:\\vertex-voice-agenda-agent\\telephony :
    .\\venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""
import asyncio
import math
import struct
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
import tempfile

import numpy as np

from gateway.agent_client import AgentError
from gateway import audiosocket as aso
from gateway.audio import resample
from gateway.call_session import CallSession
from gateway.config import load_settings
from gateway.consent import clasificar
from gateway.vad import EnergyVAD


def tono(segundos: float, amplitud: int = 8000, freq: float = 300.0) -> bytes:
    n = int(aso.SAMPLE_RATE * segundos)
    return b"".join(struct.pack("<h", int(amplitud * math.sin(2 * math.pi * freq * i / aso.SAMPLE_RATE)))
                    for i in range(n))


def silencio(segundos: float) -> bytes:
    return b"\x00\x00" * int(aso.SAMPLE_RATE * segundos)


class FakeSTT:
    def __init__(self, textos):
        self.textos = list(textos)

    def transcribe(self, pcm):
        return self.textos.pop(0) if self.textos else ""


class FakeTTS:
    def synthesize(self, text):
        return silencio(0.1)


class FakeAgent:
    """Hace de API: turnos del modelo, ficha de la llamada y los reportes que vería la UI."""

    def __init__(self, turnos, ficha_ui=None, api_caida=False):
        self.turnos = list(turnos)
        self.llamadas = []
        self.ficha_ui = ficha_ui            # {"target":..., "model_name":...} si la llamada la lanzó la UI
        self.api_caida = api_caida
        self.targets_usados = []
        self.modelos_usados = []
        self.eventos = []
        self.final = None

    async def step(self, target, call_id=None, utterance=None, model_name=None):
        self.llamadas.append((call_id, utterance))
        self.targets_usados.append(target)
        self.modelos_usados.append(model_name)
        return self.turnos.pop(0)

    async def claim(self, voice_id, fallback_target):
        if self.api_caida:
            raise AgentError("API caída")
        if self.ficha_ui:
            return {"ticket_id": "tkt_ui", **self.ficha_ui}
        return {"ticket_id": None, "target": fallback_target, "model_name": None}

    async def report_event(self, voice_id, quien, texto, tools=(), warnings=()):
        if self.api_caida:
            raise RuntimeError("API caída")
        self.eventos.append((quien, texto, list(tools)))

    async def report_end(self, voice_id, result, resumen=None):
        if self.api_caida:
            raise RuntimeError("API caída")
        self.final = result
        self.resumen = resumen

    async def heartbeat(self):
        pass


def turno(reply, terminated=False, tools=(), outcome="pending"):
    return {"call_id": "call_test", "assistant_reply": reply, "terminated": terminated,
            "tool_calls": [{"tool": t, "arguments": {}} for t in tools], "warnings": [], "outcome": outcome}


async def asterisk_falso(port, frases_contraparte, colgar_al_final=False, saludo=None):
    """Conecta como Asterisk, espera a que el agente termine de hablar y responde con 'voz' (un tono).
    saludo: segundos de 'voz' que la clínica dice al descolgar, antes de que hable el agente."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(aso.pack(aso.KIND_UUID, uuid.uuid4().bytes))
    await writer.drain()
    if saludo:
        for fr in aso.frames(tono(saludo) + silencio(0.4)):
            writer.write(aso.pack(aso.KIND_AUDIO, fr))
        await writer.drain()
    recibido = {"audio": 0, "colgado": False}
    pendientes = list(frases_contraparte)

    async def esperar_fin_de_habla():
        hablo = False
        while True:
            try:
                kind, payload = await asyncio.wait_for(aso.read_message(reader), timeout=0.4)
            except asyncio.TimeoutError:
                if hablo:
                    return True
                continue
            except asyncio.IncompleteReadError:
                return False
            if kind == aso.KIND_AUDIO:
                recibido["audio"] += 1
                hablo = True
            elif kind == aso.KIND_HANGUP:
                recibido["colgado"] = True
                return False

    while await esperar_fin_de_habla():
        if not pendientes:
            if colgar_al_final:
                writer.write(aso.pack(aso.KIND_HANGUP))
                await writer.drain()
                break
            continue
        duracion = pendientes.pop(0)
        if duracion is None:          # None = la contraparte se queda callada
            continue
        for fr in aso.frames(tono(duracion) + silencio(0.4)):
            writer.write(aso.pack(aso.KIND_AUDIO, fr))
        await writer.drain()
    writer.close()
    return recibido


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        llamada = Path(self.tmp.name) / "llamada.json"
        llamada.write_text('{"contact_name": "Clínica", "phone_or_sip_uri": "+13055550142", '
                           '"purpose": "dentist_appointment", "context_notes": "Limpieza."}', encoding="utf-8")
        base = load_settings(Path(self.tmp.name) / "no-existe.env")
        self.settings = replace(base, vad_silence_ms=200, llamada_file=llamada, logs_dir=Path(self.tmp.name) / "logs")
        self.sessions = []

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def correr(self, stt, agent, frases, wait_s=1.5, colgar_al_final=False, saludo=None):
        async def on_connect(reader, writer):
            s = CallSession(reader, writer, settings=self.settings, stt=stt, tts=FakeTTS(), agent=agent, wait_s=wait_s,
                            saludo_s=1.0)
            self.sessions.append(s)
            await s.run()

        server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            recibido = await asyncio.wait_for(asterisk_falso(port, frases, colgar_al_final, saludo), timeout=30)
            for _ in range(50):
                if self.sessions and self.sessions[0].result != "sin_iniciar" and self.sessions[0].transcript:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
        return self.sessions[0], recibido


class FlujoTests(Base):
    async def test_llamada_completa_con_consentimiento(self):
        stt = FakeSTT(["Sí, de acuerdo.", "Sí, tenemos el lunes a las 10.", "Perfecto, hasta luego."])
        agent = FakeAgent([
            turno("Hola, llamo de parte de Denis.", tools=["check_calendar"]),
            turno("Perfecto, queda el lunes a las 10.", tools=["book_appointment"]),
            turno("Gracias, hasta luego.", terminated=True, tools=["log_call_outcome", "hangup_call"],
                  outcome="appointment_booked"),
        ])
        s, recibido = await self.correr(stt, agent, [0.6, 0.6, 0.6])
        self.assertEqual(s.result, "appointment_booked")
        self.assertEqual(agent.llamadas, [(None, None), ("call_test", "Sí, tenemos el lunes a las 10."),
                                          ("call_test", "Perfecto, hasta luego.")])
        self.assertTrue(recibido["colgado"])
        self.assertTrue(recibido["audio"] > 0)
        self.assertEqual(s.transcript[0]["quien"], "agente")
        self.assertIn("inteligencia artificial", s.transcript[0]["texto"])
        self.assertTrue(list(self.settings.logs_dir.glob("*.json")))

    async def test_no_confirma_por_voz_una_cita_rechazada(self):
        rechazada = turno("Perfecto, queda a las nueve.")
        rechazada["warnings"] = ["book_appointment rechazado: el horario no está dentro de los huecos libres consultados."]
        agent = FakeAgent([turno("Hola."), rechazada, turno("Gracias.", terminated=True)])
        s, _ = await self.correr(FakeSTT(["Sí.", "A las nueve.", "Adiós."]), agent, [0.6, 0.6, 0.6])
        dichos = [t["texto"] for t in s.transcript if t["quien"] == "agente"]
        self.assertNotIn("Perfecto, queda a las nueve.", dichos)
        self.assertIn("ese horario no lo tengo libre", " ".join(dichos))

    async def test_llamada_lanzada_desde_la_ui_usa_su_ficha_y_su_modelo(self):
        ficha = {"target": {"contact_name": "Clínica de la UI", "phone_or_sip_uri": "sip:ui@pbx.local",
                            "purpose": "dentist_appointment"}, "model_name": "vertex-voice-agenda"}
        agent = FakeAgent([turno("Hola.", tools=["check_calendar"]), turno("Gracias.", terminated=True,
                                                                            outcome="no_availability")],
                          ficha_ui=ficha)
        s, _ = await self.correr(FakeSTT(["Sí.", "No hay nada."]), agent, [0.6, 0.6])
        self.assertEqual(agent.targets_usados[0]["contact_name"], "Clínica de la UI")
        self.assertEqual(set(agent.modelos_usados), {"vertex-voice-agenda"})
        await asyncio.sleep(0.2)
        quienes = [e[0] for e in agent.eventos]
        self.assertIn("agente", quienes)
        self.assertIn("contraparte", quienes)
        self.assertIn(("agente", "Hola.", ["check_calendar"]), agent.eventos)
        self.assertEqual(agent.final, "no_availability")
        # Al colgar se manda la transcripción entera con el ticket (por si la API se reinició).
        self.assertEqual(agent.resumen["ticket_id"], "tkt_ui")
        self.assertEqual(agent.resumen["contact_name"], "Clínica de la UI")
        self.assertEqual(len(agent.resumen["events"]), len(s.transcript))
        hola = next(e for e in agent.resumen["events"] if e["texto"] == "Hola.")
        self.assertEqual(hola["tools"], ["check_calendar"])

    async def test_la_clinica_saluda_primero_y_el_agente_no_la_pisa(self):
        stt = FakeSTT(["Clínica Dental Dr. Ramírez, ¿en qué puedo ayudarle?", "Sí, adelante.", "Hasta luego."])
        agent = FakeAgent([turno("Buenas, llamo de parte de Denis."), turno("Gracias.", terminated=True)])
        s, _ = await self.correr(stt, agent, [0.6, 0.6], saludo=0.8)
        self.assertEqual(s.transcript[0]["quien"], "contraparte")
        self.assertIn("Dr. Ramírez", s.transcript[0]["texto"])
        self.assertEqual(s.transcript[1]["quien"], "agente")
        self.assertIn("inteligencia artificial", s.transcript[1]["texto"])
        self.assertEqual(s.saludo, "Clínica Dental Dr. Ramírez, ¿en qué puedo ayudarle?")

    async def test_tras_sigue_ahi_no_repite_la_frase_anterior(self):
        agent = FakeAgent([turno("Hola, ¿tendrían hueco el lunes?"), turno("Gracias.", terminated=True)])
        s, _ = await self.correr(FakeSTT(["Sí.", "Sí, aquí estoy."]), agent, [0.6, None, 0.6])
        dichos = [t["texto"] for t in s.transcript if t["quien"] == "agente"]
        self.assertIn("¿Sigue ahí?", dichos)
        self.assertEqual(dichos.count("Hola, ¿tendrían hueco el lunes?"), 1)

    async def test_si_la_api_corrigio_la_respuesta_se_dice_la_suya(self):
        corregida = turno("Perdone, a esa hora no lo tengo libre, pero el viernes 27 tengo libre a las 9.")
        corregida["warnings"] = ["book_appointment rechazado: ocupado.",
                                 "Respuesta corregida por la API: se ofrecen los huecos libres reales de la agenda."]
        agent = FakeAgent([turno("Hola."), corregida, turno("Gracias.", terminated=True)])
        s, _ = await self.correr(FakeSTT(["Sí.", "A las 10.", "Adiós."]), agent, [0.6, 0.6, 0.6])
        dichos = " ".join(t["texto"] for t in s.transcript if t["quien"] == "agente")
        self.assertIn("el viernes 27 tengo libre a las 9", dichos)
        self.assertNotIn("¿Tendría otro horario disponible?", dichos)

    async def test_sin_ui_usa_llamada_json(self):
        agent = FakeAgent([turno("Hola."), turno("Gracias.", terminated=True)])
        await self.correr(FakeSTT(["Sí.", "Adiós."]), agent, [0.6, 0.6])
        self.assertEqual(agent.targets_usados[0]["contact_name"], "Clínica")
        self.assertEqual(agent.modelos_usados[0], None)

    async def test_api_de_telefonia_caida_no_tumba_la_llamada(self):
        agent = FakeAgent([turno("Hola."), turno("Gracias.", terminated=True, outcome="appointment_booked")],
                          api_caida=True)
        s, _ = await self.correr(FakeSTT(["Sí.", "Adiós."]), agent, [0.6, 0.6])
        self.assertEqual(agent.targets_usados[0]["contact_name"], "Clínica")   # cayó a llamada.json
        self.assertEqual(s.result, "appointment_booked")

    async def test_sin_consentimiento_no_llama_al_modelo(self):
        agent = FakeAgent([])
        s, recibido = await self.correr(FakeSTT(["No, prefiero que no."]), agent, [0.6])
        self.assertEqual(s.result, "sin_consentimiento")
        self.assertEqual(agent.llamadas, [])
        self.assertTrue(recibido["colgado"])

    async def test_duda_repregunta_una_vez(self):
        agent = FakeAgent([turno("Hola.", terminated=True)])
        s, _ = await self.correr(FakeSTT(["¿Cómo dice?", "Ah, sí, claro."]), agent, [0.6, 0.6])
        self.assertEqual(s.result, "pending")
        self.assertEqual(len(agent.llamadas), 1)
        textos = [t["texto"] for t in s.transcript if t["quien"] == "agente"]
        self.assertIn("Puede decir sí o no", textos[1])

    async def test_silencio_al_aviso_cuelga(self):
        agent = FakeAgent([])
        s, recibido = await self.correr(FakeSTT([]), agent, [None], wait_s=0.8)
        self.assertEqual(s.result, "sin_respuesta_al_aviso")
        self.assertEqual(agent.llamadas, [])
        self.assertTrue(recibido["colgado"])

    async def test_contraparte_cuelga_a_mitad(self):
        agent = FakeAgent([turno("Hola, llamo de parte de Denis.")])
        s, _ = await self.correr(FakeSTT(["Sí."]), agent, [0.6], colgar_al_final=True)
        self.assertEqual(s.result, "colgada_por_contraparte")


class PiezasTests(unittest.TestCase):
    def test_protocolo_ida_y_vuelta(self):
        async def ida_vuelta():
            reader = asyncio.StreamReader()
            reader.feed_data(aso.pack(aso.KIND_AUDIO, b"\x01\x02" * 160) + aso.pack(aso.KIND_HANGUP))
            reader.feed_eof()
            return [await aso.read_message(reader), await aso.read_message(reader)]
        (k1, p1), (k2, p2) = asyncio.run(ida_vuelta())
        self.assertEqual((k1, len(p1), k2, p2), (aso.KIND_AUDIO, 320, aso.KIND_HANGUP, b""))

    def test_tramas_de_20ms(self):
        tr = aso.frames(b"\x01" * 700)
        self.assertEqual([len(t) for t in tr], [320, 320, 320])

    def test_vad_detecta_una_frase_y_descarta_clics(self):
        vad = EnergyVAD(threshold=600, silence_ms=300)
        frases = [f for fr in aso.frames(silencio(0.5) + tono(0.8) + silencio(0.6)) if (f := vad.feed(fr))]
        self.assertEqual(len(frases), 1)
        self.assertGreater(len(frases[0]), aso.SAMPLE_RATE * 2 * 0.8)
        vad2 = EnergyVAD(threshold=600, silence_ms=300)
        clics = [f for fr in aso.frames(silencio(0.3) + tono(0.08) + silencio(0.6)) if (f := vad2.feed(fr))]
        self.assertEqual(clics, [])

    def test_resample(self):
        x = np.sin(np.linspace(0, 100, 22050)).astype(np.float32)
        self.assertEqual(resample(x, 22050, 8000).size, 8000)
        self.assertEqual(resample(np.zeros(8000, np.float32), 8000, 16000).size, 16000)

    def test_consentimiento(self):
        casos = {
            "Sí, claro": "si", "sí": "si", "De acuerdo, adelante": "si", "No hay problema": "si", "Cómo no": "si",
            "No": "no", "No, gracias": "no", "Prefiero que no": "no", "No estoy de acuerdo": "no",
            "¿Quién habla?": "duda", "": "duda",
        }
        for texto, esperado in casos.items():
            self.assertEqual(clasificar(texto), esperado, texto)


if __name__ == "__main__":
    unittest.main()
