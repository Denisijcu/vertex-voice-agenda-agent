"""
Pruebas del puente de telefonía sin Docker ni Asterisk: un "runner" falso contesta como docker/asterisk.

Correr desde E:\\vertex-voice-agenda-agent\\api :
    .\\venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""
import asyncio
import unittest

from fastapi.testclient import TestClient

from app import main
from app.telephony_bridge import (
    AsteriskControl,
    LineNotReady,
    RealCallRequest,
    TelephonyError,
    TelephonyService,
    TelephonyStore,
)

CONTACTOS_AVAIL = """
  Contact:  <Aor/ContactUri..............................> <Hash....> <Status> <RTT(ms)..>
==========================================================================================

  Contact:  denis/sip:denis@172.26.0.1:34784               6bf49c99f3 Avail       102.617

Objects found: 1
"""


class FakeDocker:
    def __init__(self, running=True, contact="Avail", originate_ok=True):
        self.running, self.contact, self.originate_ok = running, contact, originate_ok
        self.calls: list[list[str]] = []

    async def __call__(self, args):
        self.calls.append(args)
        if args[1] == "inspect":
            return 0, "true\n" if self.running else "false\n"
        command = args[-1]
        if command == "pjsip show contacts":
            if self.contact is None:
                return 0, "No objects found.\n"
            return 0, CONTACTOS_AVAIL.replace("Avail", self.contact)
        if command.startswith("channel originate"):
            return (0, "") if self.originate_ok else (1, "Failed to create outgoing session")
        return 1, "comando inesperado"


TARGET = {"contact_name": "Clínica Dental Coral Gables", "phone_or_sip_uri": "sip:dental@pbx.local",
          "purpose": "dentist_appointment", "context_notes": "Limpieza.", "dry_run": True}


def servicio(docker: FakeDocker, heartbeat=True, ttl=90.0) -> TelephonyService:
    store = TelephonyStore(ttl)
    if heartbeat:
        store.heartbeat()
    return TelephonyService(AsteriskControl("docker", "vva-asterisk", runner=docker), store, "denis")


def run(coro):
    return asyncio.run(coro)


class EstadoTests(unittest.TestCase):
    def test_lista_cuando_todo_esta_arriba(self):
        st = run(servicio(FakeDocker()).status())
        self.assertTrue(st["ready"])
        self.assertEqual(st["phone"], "Avail")

    def test_asterisk_parado(self):
        st = run(servicio(FakeDocker(running=False)).status())
        self.assertFalse(st["ready"])
        self.assertIn("Asterisk", st["detail"])

    def test_celular_dormido(self):
        st = run(servicio(FakeDocker(contact="Unavail")).status())
        self.assertFalse(st["ready"])
        self.assertEqual(st["phone"], "Unavail")
        self.assertIn("Linphone", st["detail"])

    def test_celular_no_registrado(self):
        st = run(servicio(FakeDocker(contact=None)).status())
        self.assertIsNone(st["phone"])
        self.assertFalse(st["ready"])

    def test_pasarela_sin_latido(self):
        st = run(servicio(FakeDocker(), heartbeat=False).status())
        self.assertFalse(st["ready"])
        self.assertIn("pasarela", st["detail"])


class LlamadaTests(unittest.TestCase):
    def test_lanzar_marca_al_celular_con_docker_exec_sin_shell(self):
        docker = FakeDocker()
        svc = servicio(docker)
        ticket = run(svc.start_call(RealCallRequest(target=TARGET, model_name="vertex-voice-agenda")))
        self.assertTrue(ticket.ticket_id.startswith("tkt_"))
        self.assertIn(["docker", "exec", "vva-asterisk", "asterisk", "-rx",
                       "channel originate PJSIP/denis extension agente@agente"], docker.calls)

    def test_no_lanza_si_la_linea_no_esta_lista(self):
        docker = FakeDocker(contact="Unavail")
        with self.assertRaises(LineNotReady):
            run(servicio(docker).start_call(RealCallRequest(target=TARGET)))
        self.assertFalse(any("originate" in a[-1] for a in docker.calls))

    def test_si_asterisk_no_marca_se_borra_el_ticket(self):
        svc = servicio(FakeDocker(originate_ok=False))
        with self.assertRaises(TelephonyError):
            run(svc.start_call(RealCallRequest(target=TARGET)))
        self.assertEqual(svc.store.tickets, {})

    def test_usuario_sip_raro_se_rechaza(self):
        ctl = AsteriskControl("docker", "vva-asterisk", runner=FakeDocker())
        with self.assertRaises(TelephonyError):
            run(ctl.originate("denis; core stop now"))

    def test_ciclo_completo_del_ticket(self):
        svc = servicio(FakeDocker())
        ticket = run(svc.start_call(RealCallRequest(target=TARGET, model_name="vertex-voice-agenda")))
        self.assertEqual(svc.store.ticket_status(ticket.ticket_id)["estado"], "llamando")
        reclamado, call = svc.store.claim("voz-1", fallback_target={"contact_name": "Otra"})
        self.assertEqual(reclamado.ticket_id, ticket.ticket_id)
        self.assertEqual(call.target["contact_name"], "Clínica Dental Coral Gables")
        self.assertEqual(svc.store.ticket_status(ticket.ticket_id)["estado"], "en_curso")
        from app.telephony_bridge import VoiceEnd, VoiceEvent
        svc.store.add_event("voz-1", VoiceEvent(quien="agente", texto="Hola.", tools=["check_calendar"]))
        svc.store.finish(VoiceEnd(result="appointment_booked"), "voz-1")
        st = svc.store.ticket_status(ticket.ticket_id)
        self.assertEqual((st["estado"], st["call"]["result"]), ("terminada", "appointment_booked"))
        self.assertEqual(st["call"]["events"][0]["tools"], ["check_calendar"])

    def test_llamada_entrante_usa_la_ficha_de_la_pasarela(self):
        store = TelephonyStore()
        ticket, call = store.claim("voz-2", fallback_target={"contact_name": "De llamada.json"})
        self.assertIsNone(ticket)
        self.assertEqual(call.target["contact_name"], "De llamada.json")

    def test_ticket_sin_contestar_caduca(self):
        store = TelephonyStore(ticket_ttl_s=15)
        t = store.create_ticket(TARGET, None)
        t.created -= 100
        self.assertEqual(store.ticket_status(t.ticket_id)["estado"], "sin_contestar")
        ticket, _ = store.claim("voz-3", fallback_target=None)
        self.assertIsNone(ticket)          # un ticket caducado no se entrega a otra llamada


class ApiTelefoniaTests(unittest.TestCase):
    def setUp(self):
        self.docker = FakeDocker()
        main._telephony = servicio(self.docker)
        self.client = TestClient(main.app)

    def test_flujo_por_http(self):
        self.assertTrue(self.client.get("/api/v1/telephony/status").json()["ready"])
        r = self.client.post("/api/v1/telephony/calls", json={"target": TARGET, "model_name": "vertex-voice-agenda"})
        self.assertEqual(r.status_code, 200, r.text)
        ticket_id = r.json()["ticket_id"]
        c = self.client.post("/api/v1/telephony/claim", json={"voice_id": "voz-9"}).json()
        self.assertEqual((c["ticket_id"], c["model_name"]), (ticket_id, "vertex-voice-agenda"))
        self.assertEqual(c["target"]["contact_name"], "Clínica Dental Coral Gables")
        e = self.client.post("/api/v1/telephony/calls/voz-9/events",
                             json={"quien": "contraparte", "texto": "Sí, de acuerdo."})
        self.assertEqual(e.status_code, 200)
        self.client.post("/api/v1/telephony/calls/voz-9/end", json={"result": "appointment_booked"})
        st = self.client.get(f"/api/v1/telephony/tickets/{ticket_id}").json()
        self.assertEqual(st["estado"], "terminada")
        self.assertEqual(self.client.get("/api/v1/telephony/calls").json()["items"][0]["result"], "appointment_booked")

    def test_si_la_api_se_reinicia_a_mitad_la_llamada_se_recupera_al_colgar(self):
        r = self.client.post("/api/v1/telephony/calls", json={"target": TARGET})
        ticket_id = r.json()["ticket_id"]
        self.client.post("/api/v1/telephony/claim", json={"voice_id": "voz-r"})
        main._telephony = servicio(self.docker)            # reinicio: la memoria se pierde
        self.assertEqual(self.client.get(f"/api/v1/telephony/tickets/{ticket_id}").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/telephony/calls/voz-r/events",
                                          json={"quien": "agente", "texto": "x"}).status_code, 404)
        fin = {"result": "appointment_booked", "ticket_id": ticket_id, "contact_name": "Clínica Dental Coral Gables",
               "purpose": "dentist_appointment", "events": [
                   {"quien": "agente", "texto": "Hola.", "t": "2026-11-21T15:05:25-05:00"},
                   {"quien": "contraparte", "texto": "Sí, adelante."},
                   {"quien": "agente", "texto": "Queda el lunes a las 9.", "tools": ["book_appointment"]}]}
        self.assertEqual(self.client.post("/api/v1/telephony/calls/voz-r/end", json=fin).status_code, 200)
        st = self.client.get(f"/api/v1/telephony/tickets/{ticket_id}").json()
        self.assertEqual((st["estado"], st["call"]["result"]), ("terminada", "appointment_booked"))
        self.assertEqual(len(st["call"]["events"]), 3)
        self.assertEqual(st["call"]["events"][2]["tools"], ["book_appointment"])
        self.assertEqual(self.client.get("/api/v1/telephony/calls").json()["items"][0]["contact_name"],
                         "Clínica Dental Coral Gables")

    def test_fin_sin_resumen_de_llamada_desconocida_es_404(self):
        self.assertEqual(self.client.post("/api/v1/telephony/calls/nada/end", json={"result": "x"}).status_code, 404)

    def test_borrar_por_http(self):
        self.client.post("/api/v1/telephony/claim", json={"voice_id": "voz-b"})
        self.assertEqual(self.client.delete("/api/v1/telephony/calls/voz-b").status_code, 409)   # en curso
        self.client.post("/api/v1/telephony/calls/voz-b/end", json={"result": "colgada"})
        self.assertEqual(self.client.delete("/api/v1/telephony/calls/voz-b").status_code, 200)
        self.assertEqual(self.client.delete("/api/v1/telephony/calls/voz-b").status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/telephony/calls").status_code, 400)          # sin confirm
        self.assertEqual(self.client.delete("/api/v1/telephony/calls?confirm=true").json()["ok"], True)

    def test_el_navegador_puede_borrar_cors(self):
        origen = main.settings.allowed_origins[0]
        r = self.client.options("/api/v1/telephony/calls/x",
                                headers={"Origin": origen, "Access-Control-Request-Method": "DELETE"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("DELETE", r.headers["access-control-allow-methods"])

    def test_409_si_el_celular_no_esta(self):
        self.docker.contact = "Unavail"
        r = self.client.post("/api/v1/telephony/calls", json={"target": TARGET})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Linphone", r.json()["detail"])

    def test_evento_de_llamada_desconocida(self):
        r = self.client.post("/api/v1/telephony/calls/no-existe/events", json={"quien": "agente", "texto": "x"})
        self.assertEqual(r.status_code, 404)

    def test_latido_de_la_pasarela(self):
        main._telephony = servicio(self.docker, heartbeat=False)
        self.assertFalse(self.client.get("/api/v1/telephony/status").json()["gateway"])
        self.client.post("/api/v1/telephony/heartbeat")
        main._telephony._status_cache = None
        self.assertTrue(self.client.get("/api/v1/telephony/status").json()["gateway"])


class HistorialTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from app.call_history import CallHistory
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.history = CallHistory(self.dir / "data" / "llamadas.sqlite3")

    def tearDown(self):
        self.history._db.close()
        self.tmp.cleanup()

    def test_la_llamada_se_guarda_y_sobrevive_a_un_reinicio(self):
        from app.call_history import CallHistory
        from app.telephony_bridge import VoiceEnd, VoiceEvent
        store = TelephonyStore(history=self.history)
        store.create_ticket(TARGET, "vertex-voice-agenda")
        store.claim("voz-h", None)
        store.add_event("voz-h", VoiceEvent(quien="contraparte", texto="Clínica Dental, ¿en qué puedo ayudarle?"))
        store.add_event("voz-h", VoiceEvent(quien="agente", texto="Hola.", tools=["check_calendar"]))
        store.finish(VoiceEnd(result="appointment_booked"), "voz-h")
        otro = CallHistory(self.dir / "data" / "llamadas.sqlite3")        # "reinicio" de la API
        lista = otro.list()
        self.assertEqual(lista["total"], 1)
        fila = lista["items"][0]
        self.assertEqual((fila["contact_name"], fila["result"], fila["turnos"], fila["origen"]),
                         ("Clínica Dental Coral Gables", "appointment_booked", 2, "ui"))
        detalle = otro.get("voz-h")
        self.assertEqual(detalle["events"][1]["tools"], ["check_calendar"])
        self.assertIsNotNone(detalle["ended"])
        otro._db.close()

    def test_borrar_una_y_todas_sin_que_resuciten_al_importar(self):
        import json
        logs = self.dir / "logs"
        logs.mkdir()
        for i in range(3):
            (logs / f"20261121-15000{i}_v{i}.json").write_text(json.dumps(
                {"uuid": f"v{i}", "resultado": "colgada", "turnos": [{"t": "2026-11-21T15:00:00", "quien": "agente",
                                                                     "texto": "Hola."}]}), encoding="utf-8")
        self.history.import_gateway_logs(logs)
        store = TelephonyStore(history=self.history)
        self.assertTrue(store.delete_call("v0"))
        self.assertFalse(store.delete_call("v0"))
        self.assertEqual(self.history.import_gateway_logs(logs), 0)        # no vuelve
        self.assertEqual(self.history.list()["total"], 2)
        self.assertEqual(store.delete_all_calls(), 2)
        self.assertEqual(self.history.import_gateway_logs(logs), 0)
        self.assertEqual(self.history.list()["total"], 0)

    def test_no_se_borra_una_llamada_en_curso(self):
        store = TelephonyStore(history=self.history)
        store.claim("viva", {"contact_name": "Clínica"})
        with self.assertRaises(ValueError):
            store.delete_call("viva")
        self.assertEqual(store.delete_all_calls(), 0)
        self.assertEqual(self.history.list()["total"], 1)

    def test_busqueda_por_clinica_resultado_o_frase(self):
        for i, (clinica, resultado, frase) in enumerate([("Dr. Ramírez", "appointment_booked", "el martes a las 9"),
                                                          ("Coral Gables", "rejected_or_unavailable", "está a tope")]):
            self.history.save({"voice_id": f"v{i}", "started": f"2026-11-21T15:0{i}:00-05:00", "status": "terminada",
                               "result": resultado, "contact_name": clinica,
                               "events": [{"quien": "contraparte", "texto": frase}]}, origen="ui")
        self.assertEqual(self.history.list(q="ramírez")["total"], 1)
        self.assertEqual(self.history.list(q="rejected")["items"][0]["voice_id"], "v1")
        self.assertEqual(self.history.list(q="a tope")["items"][0]["voice_id"], "v1")
        self.assertEqual([i["voice_id"] for i in self.history.list()["items"]], ["v1", "v0"])   # nuevas primero
        self.assertEqual(self.history.list(limit=1, offset=1)["items"][0]["voice_id"], "v0")

    def test_importa_las_transcripciones_de_la_pasarela_sin_duplicar(self):
        import json
        logs = self.dir / "logs"
        logs.mkdir()
        (logs / "20261121-150617_9285c375.json").write_text(json.dumps({
            "uuid": "9285c375-85bc", "resultado": "appointment_booked",
            "turnos": [{"t": "2026-11-21T15:05:25", "quien": "agente", "texto": "Hola."},
                       {"t": "2026-11-21T15:05:41", "quien": "contraparte", "texto": "Sí, adelante."}]}),
            encoding="utf-8")
        self.assertEqual(self.history.import_gateway_logs(logs), 1)
        self.assertEqual(self.history.import_gateway_logs(logs), 0)
        fila = self.history.list()["items"][0]
        self.assertEqual((fila["origen"], fila["turnos"], fila["result"]), ("importada", 2, "appointment_booked"))
        self.assertTrue(fila["started"].startswith("2026-11-21T15:05:25"))
        self.assertTrue(fila["ended"].startswith("2026-11-21T15:05:41"))


if __name__ == "__main__":
    unittest.main()
