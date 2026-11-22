"""
Pruebas del orquestador sin LM Studio: un transporte falso de httpx devuelve las respuestas del "modelo".
Solo usan la librería estándar (unittest), no hace falta instalar pytest.

Correr desde E:\\vertex-voice-agenda-agent\\api :
    .\\venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import asyncio
import json
import unittest
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app import main
from app.call_orchestrator import CallFinishedError, CallInputError, CallOrchestrator
from app.policy import SYSTEM_CALL_POLICY
from app.schemas import CallPurpose, StartCallRequest
from app.timeutil import local_now, next_business_day_window


class FakeLLM:
    """Devuelve en orden las respuestas guionizadas y guarda lo que se le envió."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        # Si se acaban las respuestas, repite la última (así se prueba el reintento con un modelo que sigue mal).
        content = self.replies.pop(0) if self.replies else self._ultima
        self._ultima = content
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def window():
    return next_business_day_window(local_now(), 9, 12)


def req(call_id=None, utterance=None, dry_run=True):
    start, end = window()
    return StartCallRequest(
        call_id=call_id,
        target={
            "contact_name": "Clínica Dental Brisa",
            "phone_or_sip_uri": "+13055550142",
            "purpose": "dentist_appointment",
            "context_notes": "Limpieza dental para Denis Sánchez.",
            "preferred_window_start": start,
            "preferred_window_end": end,
            "dry_run": dry_run,
        },
        caller_utterance=utterance,
    )


def check_call():
    start, end = window()
    return {"tool": "check_calendar", "arguments": {"start_iso": start.isoformat(), "end_iso": end.isoformat()}}


def book_call(start: datetime, minutes=30):
    return {
        "tool": "book_appointment",
        "arguments": {
            "title": "Limpieza dental - Clínica Dental Brisa",
            "start_iso": start.isoformat(),
            "end_iso": (start + timedelta(minutes=minutes)).isoformat(),
            "attendee_or_location": "Clínica Dental Brisa",
            "purpose": "dentist_appointment",
        },
    }


def run(coro):
    return asyncio.run(coro)


class OrchestratorTests(unittest.TestCase):
    def test_primer_turno_consulta_la_ventana_y_usa_el_prompt_unico(self):
        llm = FakeLLM({"assistant_reply": "Hola, llamo de parte de Denis Sánchez.", "tool_calls": [check_call()]})
        orch = CallOrchestrator(transport=llm.transport())
        res = run(orch.start_or_step_call(req()))
        self.assertEqual(res.outcome.value, "pending")
        self.assertEqual([c.tool for c in res.tool_calls], ["check_calendar"])
        self.assertEqual(res.warnings, [])
        enviado = llm.requests[0]["messages"]
        self.assertEqual(enviado[0]["content"], SYSTEM_CALL_POLICY)
        self.assertIn("Ventana a consultar:", enviado[1]["content"])
        self.assertTrue(res.dry_run)

    def test_agenda_un_hueco_libre_ofrecido(self):
        start, _ = window()
        llm = FakeLLM(
            {"assistant_reply": "Hola.", "tool_calls": [check_call()]},
            {"assistant_reply": "Perfecto, la agendo a las 9:30.", "tool_calls": [book_call(start + timedelta(minutes=30))]},
        )
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Tenemos a las 9:30, ¿le sirve?")))
        self.assertEqual(r2.outcome.value, "appointment_booked")
        self.assertEqual(r2.warnings, [])
        seguimiento = llm.requests[1]["messages"][-1]["content"]
        self.assertIn("Huecos libres detectados:", seguimiento)
        # el turno anterior del modelo quedó en el historial como JSON, igual que en el entrenamiento
        json.loads(llm.requests[1]["messages"][2]["content"])

    def test_agenda_hora_libre_que_propone_la_clinica_aunque_no_se_consultara(self):
        start, _ = window()
        llm = FakeLLM(
            {"assistant_reply": "Hola.", "tool_calls": [check_call()]},
            {"assistant_reply": "Listo a las 3 de la tarde.", "tool_calls": [book_call(start.replace(hour=15))]},
        )
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Solo tengo a las 3 de la tarde, ¿le sirve?")))
        self.assertEqual(r2.outcome.value, "appointment_booked", r2.warnings)

    def test_hora_ocupada_o_de_noche_se_rechaza_y_ofrece_huecos_reales(self):
        start, _ = window()
        from app.schemas import BookAppointmentArgs
        for pedido in (start.replace(hour=20), start.replace(hour=10)):
            llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                          {"assistant_reply": "Queda.", "tool_calls": [book_call(pedido)]})
            orch = CallOrchestrator(transport=llm.transport())
            orch.calendar.book_appointment(BookAppointmentArgs(**book_call(start.replace(hour=10))["arguments"]),
                                           dry_run=True)                          # las 10 ya están ocupadas
            r1 = run(orch.start_or_step_call(req()))
            r2 = run(orch.start_or_step_call(req(r1.call_id, "Sí, a esa hora le anoto.")))
            self.assertEqual(r2.outcome.value, "pending")
            self.assertTrue(r2.assistant_reply.startswith("Perdone, a esa hora no lo tengo libre, pero "), r2.assistant_reply)
            self.assertIn("a las 9", r2.assistant_reply)
            self.assertTrue(any(w.startswith("Respuesta corregida por la API") for w in r2.warnings))

    def test_si_el_modelo_dice_que_no_sin_mirar_se_mira_el_dia_de_la_clinica(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Lo siento, ese horario no está libre. ¿Podría ser a las 9?", "tool_calls": []})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Señorita, tengo para el viernes en la tarde.")))
        self.assertIn("viernes", r2.assistant_reply)
        self.assertIn("de la tarde", r2.assistant_reply)
        self.assertNotIn("no está libre", r2.assistant_reply)
        self.assertEqual([c.tool for c in r2.tool_calls], ["check_calendar"])

    def test_no_a_ese_dia_ofrece_otros_dias(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Déjeme ver.", "tool_calls": [check_call()]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        dia, _ = window()
        r2 = run(orch.start_or_step_call(req(r1.call_id, f"No, el lunes {dia.day}." if dia.weekday() == 0
                                             else f"No, el {dia.day}.")))
        self.assertTrue(r2.assistant_reply.startswith("Entendido, ese día no."), r2.assistant_reply)
        self.assertNotIn(f" {dia.day} ", r2.assistant_reply.split("Entonces")[1])

    def test_dia_mencionado(self):
        from app.call_orchestrator import _dia_mencionado
        hoy = datetime(2026, 11, 21, 17, 0).astimezone()                       # sábado 21
        self.assertEqual(_dia_mencionado("tengo para el viernes en la tarde", hoy)[0].day, 27)
        self.assertEqual(_dia_mencionado("tengo para el viernes en la tarde", hoy)[1], (13, 17))
        self.assertEqual(_dia_mencionado("No, el lunes 23.", hoy)[0].day, 23)
        self.assertEqual(_dia_mencionado("Si tengo para el viernes 27", hoy)[0].day, 27)
        self.assertEqual(_dia_mencionado("mañana por la mañana a las 9", hoy), ((hoy + timedelta(days=1)).date(), (9, 12)))
        self.assertEqual(_dia_mencionado("a las 10 de la mañana", hoy), (None, (9, 12)))

    def test_nunca_agenda_solo_si_el_modelo_no_lo_pide(self):
        llm = FakeLLM(
            {"assistant_reply": "Hola.", "tool_calls": [check_call()]},
            {"assistant_reply": "Entiendo, ¿qué otro día le vendría bien?", "tool_calls": []},
        )
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "No, esta semana no podemos.")))
        self.assertEqual(r2.tool_calls, [])
        self.assertEqual(r2.outcome.value, "pending")
        self.assertEqual(orch.calendar.list_events(), [])

    def test_json_invalido_no_se_lee_por_telefono(self):
        llm = FakeLLM('{"assistant_reply": "Hola", "tool_calls": [')
        orch = CallOrchestrator(transport=llm.transport())
        res = run(orch.start_or_step_call(req()))
        self.assertNotIn("{", res.assistant_reply)
        self.assertTrue(any("JSON" in w for w in res.warnings))
        # aun así el primer turno consulta la agenda
        self.assertEqual([c.tool for c in res.tool_calls], ["check_calendar"])

    def test_basura_del_modelo_no_se_lee_por_telefono(self):
        llm = FakeLLM("<tool_call> " * 200)
        orch = CallOrchestrator(transport=llm.transport())
        res = run(orch.start_or_step_call(req()))
        self.assertNotIn("tool_call", res.assistant_reply)
        self.assertTrue(any("JSON" in w and "Llegó:" in w for w in res.warnings))

    def test_manda_los_ajustes_de_muestreo_de_lm_studio(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]})
        orch = CallOrchestrator(transport=llm.transport())
        run(orch.start_or_step_call(req()))
        enviado = llm.requests[0]
        self.assertEqual((enviado["top_p"], enviado["top_k"], enviado["repeat_penalty"]), (0.95, 40, 1.1))
        self.assertIn("<tool_call>", enviado["stop"])
        esquema = enviado["response_format"]["json_schema"]["schema"]
        self.assertEqual(esquema["required"], ["assistant_reply", "tool_calls"])
        self.assertIn("hangup_call", esquema["properties"]["tool_calls"]["items"]["properties"]["tool"]["enum"])

    def test_texto_suelto_sin_json_tampoco_se_lee(self):
        llm = FakeLLM("Claro, le agendo la cita para mañana.")
        orch = CallOrchestrator(transport=llm.transport())
        res = run(orch.start_or_step_call(req()))
        self.assertEqual(res.assistant_reply, "Disculpe, ¿me lo podría repetir, por favor?")

    def test_fecha_sin_zona_no_da_error(self):
        start, end = window()
        naive = {"tool": "check_calendar", "arguments": {
            "start_iso": start.replace(tzinfo=None).isoformat(), "end_iso": end.replace(tzinfo=None).isoformat()}}
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [naive]})
        orch = CallOrchestrator(transport=llm.transport())
        res = run(orch.start_or_step_call(req()))
        self.assertEqual(res.warnings, [])
        self.assertIsNotNone(res.tool_calls[0].arguments.start_iso.tzinfo)

    def test_colgar_cierra_la_llamada(self):
        llm = FakeLLM(
            {"assistant_reply": "Hola.", "tool_calls": [check_call()]},
            {"assistant_reply": "Gracias, hasta luego.", "tool_calls": [
                {"tool": "log_call_outcome", "arguments": {"outcome": "rejected_or_unavailable", "summary": "No interesa."}},
                {"tool": "hangup_call", "arguments": {"reason": "Fin", "send_sip_bye": True}},
            ]},
        )
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "No nos interesa, gracias.")))
        self.assertTrue(r2.terminated)
        self.assertEqual(r2.outcome.value, "rejected_or_unavailable")
        self.assertEqual(r2.session.sip_state.value, "terminated")
        with self.assertRaises(CallFinishedError):
            run(orch.start_or_step_call(req(r1.call_id, "¿Hola?")))

    CIERRE = {"assistant_reply": "De acuerdo, muchas gracias. Hasta luego.", "tool_calls": [
        {"tool": "log_call_outcome", "arguments": {"outcome": "rejected_or_unavailable", "summary": "Sin hueco."}},
        {"tool": "hangup_call", "arguments": {"reason": "Sin disponibilidad", "send_sip_bye": True}},
    ]}

    def test_si_no_hay_hueco_insiste_una_vez_con_otros_horarios(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]}, self.CIERRE, self.CIERRE)
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "No, no tengo, estoy ocupado en esa.")))
        self.assertFalse(r2.terminated)
        self.assertEqual(r2.outcome.value, "pending")
        self.assertIn("otro momento", r2.assistant_reply)
        self.assertIn("de la tarde", r2.assistant_reply)          # el resto del día de la llamada
        self.assertEqual([c.tool for c in r2.tool_calls], ["check_calendar"])
        self.assertTrue(any("se insistió una vez" in w for w in r2.warnings))
        # El modelo ve los horarios ofrecidos y en su historial queda lo que de verdad se dijo.
        ultimo = llm.requests[-1]["messages"]
        self.assertNotIn("Hasta luego", ultimo[-1]["content"])
        # Segundo "no": ya no se insiste.
        r3 = run(orch.start_or_step_call(req(r1.call_id, "Tampoco, esa semana estamos llenos.")))
        self.assertTrue(r3.terminated)
        self.assertEqual(r3.outcome.value, "rejected_or_unavailable")
        self.assertIn("Huecos libres detectados", llm.requests[-1]["messages"][-1]["content"])

    def test_tras_insistir_se_puede_agendar_otro_dia(self):
        start, _ = window()
        tarde = start.replace(hour=14, minute=0)
        llm = FakeLLM(
            {"assistant_reply": "Hola.", "tool_calls": [check_call()]},
            self.CIERRE,
            {"assistant_reply": "Perfecto, queda a las dos.", "tool_calls": [book_call(tarde)]},
        )
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        run(orch.start_or_step_call(req(r1.call_id, "No, por la mañana no tengo nada.")))
        r3 = run(orch.start_or_step_call(req(r1.call_id, "Sí, a las dos de la tarde le sirve.")))
        self.assertEqual(r3.warnings, [])
        self.assertEqual(r3.outcome.value, "appointment_booked")

    def test_no_insiste_con_rechazo_firme_ni_en_marketing(self):
        for frase, proposito in (("No me interesa, gracias.", "dentist_appointment"),
                                 ("No tengo tiempo ahora.", "client_marketing")):
            llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]}, self.CIERRE)
            orch = CallOrchestrator(transport=llm.transport())
            r = req()
            r.target.purpose = CallPurpose(proposito)
            r1 = run(orch.start_or_step_call(r))
            r2_req = req(r1.call_id, frase)
            r2_req.target.purpose = CallPurpose(proposito)
            r2 = run(orch.start_or_step_call(r2_req))
            self.assertTrue(r2.terminated, frase)

    def test_no_insiste_si_piden_que_devuelvan_la_llamada(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Perfecto, le llamamos luego.", "tool_calls": [
                          {"tool": "log_call_outcome", "arguments": {"outcome": "callback_requested", "summary": "x"}},
                          {"tool": "hangup_call", "arguments": {"reason": "Devolver", "send_sip_bye": True}}]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Llame mañana que hoy no está la recepcionista.")))
        self.assertTrue(r2.terminated)
        self.assertEqual(r2.outcome.value, "callback_requested")

    def test_confirma_la_cita_con_el_dia_real_de_la_agenda(self):
        start, _ = window()
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Excelente, lo dejamos el domingo 99 a las 9.", "tool_calls": [book_call(start)]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Sí, a las 9 tenemos.")))
        from app.call_orchestrator import _DIAS
        self.assertEqual(r2.outcome.value, "appointment_booked")
        self.assertIn(f"{_DIAS[start.weekday()]} {start.day} a las 9 de la mañana", r2.assistant_reply)
        self.assertNotIn("domingo 99", r2.assistant_reply)

    def test_corrige_el_dia_de_la_semana(self):
        from app.call_orchestrator import _corregir_dias
        hoy = datetime(2026, 11, 21, 15, 0).astimezone()          # sábado 21
        self.assertEqual(_corregir_dias("Déjeme revisar el miércoles 27 por la tarde.", hoy),
                         "Déjeme revisar el viernes 27 por la tarde.")
        self.assertEqual(_corregir_dias("¿El lunes 23 por la mañana?", hoy), "¿El lunes 23 por la mañana?")
        self.assertEqual(_corregir_dias("Martes 2 a las 9", hoy), "Miércoles 2 a las 9")   # 2 de diciembre
        self.assertEqual(_corregir_dias("a las 3 de la tarde", hoy), "a las 3 de la tarde")

    def test_despedida_con_json_roto_no_pide_que_repita(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]}, '{"assistant_reply": "Gr')
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Gracias por llamar a la clínica.")))
        self.assertTrue(r2.terminated)
        self.assertIn("hasta luego", r2.assistant_reply)
        self.assertNotIn("repetir", r2.assistant_reply)

    def test_consulta_a_mitad_ofrece_los_huecos_en_el_mismo_turno(self):
        start, _ = window()
        noche = start.replace(hour=20)
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Déjeme revisar a las 8 de la noche.", "tool_calls": [
                          {"tool": "check_calendar", "arguments": {"start_iso": noche.isoformat(),
                                                                   "end_iso": (noche + timedelta(hours=1)).isoformat()}}]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Ese día a las nueve y media.")))
        self.assertNotIn("noche", r2.assistant_reply)
        self.assertIn("a las 9 o a las 9 y media de la mañana", r2.assistant_reply)
        huecos = orch._calls[r1.call_id].checked_slots
        self.assertTrue(all(9 <= datetime.fromisoformat(h["start_iso"]).hour < 17 for h in huecos))

    def test_despedirse_no_borra_una_cita_agendada(self):
        start, _ = window()
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Queda a las 9.", "tool_calls": [book_call(start)]},
                      {"assistant_reply": "Gracias, hasta luego.", "tool_calls": [
                          {"tool": "log_call_outcome", "arguments": {"outcome": "rejected_or_unavailable", "summary": "x"}},
                          {"tool": "hangup_call", "arguments": {"reason": "Fin", "send_sip_bye": True}}]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        run(orch.start_or_step_call(req(r1.call_id, "A las 9 sí.")))
        r3 = run(orch.start_or_step_call(req(r1.call_id, "Gracias por llamar a la clínica.")))
        self.assertTrue(r3.terminated)
        self.assertEqual(r3.outcome.value, "appointment_booked")
        self.assertTrue(any("ya se agendó" in w for w in r3.warnings))

    def test_consulta_llena_mira_el_resto_del_dia_antes_de_decir_que_no_hay(self):
        start, _ = window()
        orch = None
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Déjeme revisar a las 9.", "tool_calls": [
                          {"tool": "check_calendar", "arguments": {"start_iso": start.isoformat(),
                                                                   "end_iso": (start + timedelta(minutes=30)).isoformat()}}]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        # Las 9:00 ya están ocupadas por otra cita.
        from app.schemas import BookAppointmentArgs
        orch.calendar.book_appointment(BookAppointmentArgs(**book_call(start)["arguments"]), dry_run=True)
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Ok, ¿a qué hora le conviene?")))
        self.assertNotIn("no tengo ningún hueco", r2.assistant_reply)
        self.assertIn("a las 9 y media", r2.assistant_reply)

    def test_dia_entero_lleno_ofrece_el_siguiente(self):
        start, _ = window()
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Déjeme revisar.", "tool_calls": [check_call()]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        from app.schemas import BookAppointmentArgs
        args = book_call(start.replace(hour=9), minutes=8 * 60)["arguments"]
        orch.calendar.book_appointment(BookAppointmentArgs(**args), dry_run=True)    # todo el día ocupado
        r2 = run(orch.start_or_step_call(req(r1.call_id, "¿Qué día le viene bien?")))
        self.assertTrue(r2.assistant_reply.startswith("Ese día no me queda nada libre, pero "), r2.assistant_reply)
        self.assertIn("a las 9", r2.assistant_reply)

    def test_no_inventa_datos_de_quien_encarga_la_llamada(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Ah, Denis Sánchez. ¿Y él qué cosa es él?")))
        self.assertIn("no tengo más datos personales", r2.assistant_reply)
        r3 = run(orch.start_or_step_call(req(r1.call_id, "¿Cómo dijo que se llama usted?")))
        self.assertIn("asistente de inteligencia artificial", r3.assistant_reply)
        self.assertEqual(len(llm.requests), 1)        # el modelo no contestó esas dos
        r4 = run(orch.start_or_step_call(req(r1.call_id, "¿Cómo dijo que se llamaba el paciente?")))
        self.assertEqual(r4.assistant_reply, "El paciente se llama Denis Sánchez.")
        r5 = run(orch.start_or_step_call(req(r1.call_id, "¿A nombre de quién pongo la cita?")))
        self.assertEqual(r5.assistant_reply, "El paciente se llama Denis Sánchez.")

    def test_json_roto_se_reintenta_una_vez_y_se_agenda(self):
        start, _ = window()
        bueno = {"assistant_reply": "Perfecto, queda a las 9.", "tool_calls": [book_call(start)]}
        roto = json.dumps(bueno, ensure_ascii=False)[:90]
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]}, roto, bueno)
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "El lunes a las nueve, le anoto.")))
        self.assertEqual(r2.outcome.value, "appointment_booked")
        self.assertNotIn("repetir", r2.assistant_reply)
        self.assertTrue(any("se le volvió a preguntar y respondió bien" in w for w in r2.warnings))

    def test_json_seguido_de_basura_se_aprovecha(self):
        from app.call_orchestrator import _extract_json_payload
        self.assertEqual(_extract_json_payload('{"assistant_reply": "Hola", "tool_calls": []} <tool_call> xx')["assistant_reply"],
                         "Hola")

    def test_cita_con_duracion_larga_o_en_utc_se_ajusta_al_hueco(self):
        start, _ = window()
        from datetime import timezone
        utc = start.replace(tzinfo=None).replace(tzinfo=timezone.utc)      # "09:00Z" queriendo decir 9 local
        for pedido in (book_call(start, minutes=240), book_call(utc)):
            llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                          {"assistant_reply": "Queda a las 9.", "tool_calls": [pedido]})
            orch = CallOrchestrator(transport=llm.transport())
            r1 = run(orch.start_or_step_call(req()))
            r2 = run(orch.start_or_step_call(req(r1.call_id, "Sí, a las 9.")))
            self.assertEqual(r2.outcome.value, "appointment_booked", r2.warnings)
            self.assertTrue(any("ajustado" in w for w in r2.warnings))

    def test_no_agenda_si_la_clinica_solo_repite_el_dia(self):
        start, _ = window()
        diez = start.replace(hour=10)
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Excelente, lo dejamos a las 10.", "tool_calls": [book_call(diez)]},
                      {"assistant_reply": "Perfecto, queda a las 10.", "tool_calls": [book_call(diez)]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        r2 = run(orch.start_or_step_call(req(r1.call_id, "El lunes, 23, por la mañana.")))
        self.assertEqual(r2.outcome.value, "pending")
        self.assertIn("le vendría bien", r2.assistant_reply)
        self.assertIn("a las 10 de la mañana", r2.assistant_reply)
        self.assertEqual(orch.calendar.list_events(), [])
        r3 = run(orch.start_or_step_call(req(r1.call_id, "Sí, a las 10 está bien.")))
        self.assertEqual(r3.outcome.value, "appointment_booked")

    def test_si_la_clinica_dice_que_no_tras_agendar_se_anula(self):
        start, _ = window()
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]},
                      {"assistant_reply": "Queda a las 9.", "tool_calls": [book_call(start)]},
                      self.CIERRE, self.CIERRE)
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        run(orch.start_or_step_call(req(r1.call_id, "Sí, a las 9.")))
        self.assertEqual(len(orch.calendar.list_events()), 1)
        r3 = run(orch.start_or_step_call(req(r1.call_id, "No, señorita, le digo que no es posible, no tengo espacio.")))
        self.assertEqual(orch.calendar.list_events(), [])
        self.assertTrue(any("se anuló" in w for w in r3.warnings))
        self.assertNotEqual(r3.outcome.value, "appointment_booked")

    def test_aceptacion_y_negativa(self):
        from app.call_orchestrator import _es_aceptacion, _es_negativa
        for t in ("Sí, de acuerdo.", "Ok, el jueves 26 a las nueve.", "Le anoto para esa fecha.", "Tenemos a las 3."):
            self.assertTrue(_es_aceptacion(t), t)
        for t in ("El lunes, 23, por la mañana.", "No, no es posible.", "Casi todo está lleno.", "¿A qué hora?"):
            self.assertFalse(_es_aceptacion(t), t)
        self.assertTrue(_es_negativa("No, señorita, ese día estamos a tope."))
        self.assertFalse(_es_negativa("Nos vemos el lunes."))

    def test_seguimiento_sin_respuesta_de_contraparte(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]})
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        with self.assertRaises(CallInputError):
            run(orch.start_or_step_call(req(r1.call_id, "   ")))

    def test_dry_run_false_sigue_simulando_sin_puente_sip(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [check_call()]})
        orch = CallOrchestrator(transport=llm.transport())
        res = run(orch.start_or_step_call(req(dry_run=False)))
        self.assertTrue(res.dry_run)

    def test_herramienta_no_permitida(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [
            {"tool": "run_shell", "arguments": {"cmd": "dir"}}, check_call()]})
        orch = CallOrchestrator(transport=llm.transport())
        res = run(orch.start_or_step_call(req()))
        self.assertEqual([c.tool for c in res.tool_calls], ["check_calendar"])
        self.assertTrue(any("no permitida" in w for w in res.warnings))


    def test_acepta_tool_calls_estilo_openai_y_con_name(self):
        start, _ = window()
        chk = check_call()
        openai = {"type": "function", "function": {"name": "check_calendar",
                                                   "arguments": json.dumps(chk["arguments"])}}
        reserva = book_call(start)
        con_name = {"name": "book_appointment", "arguments": reserva["arguments"]}
        llm = FakeLLM(
            {"assistant_reply": "Hola.", "tool_calls": [openai]},
            {"assistant_reply": "Perfecto, la agendo.", "tool_calls": [con_name]},
        )
        orch = CallOrchestrator(transport=llm.transport())
        r1 = run(orch.start_or_step_call(req()))
        self.assertEqual(r1.warnings, [])
        r2 = run(orch.start_or_step_call(req(r1.call_id, "Sí, a las 9 está bien.")))
        self.assertEqual(r2.warnings, [])
        self.assertEqual(r2.outcome.value, "appointment_booked")
        # en el historial queda en el formato de entrenamiento
        historial = json.loads(llm.requests[1]["messages"][2]["content"])
        self.assertEqual(historial["tool_calls"][0]["tool"], "check_calendar")

    def test_aviso_muestra_lo_que_llego(self):
        llm = FakeLLM({"assistant_reply": "Hola.", "tool_calls": [{"accion": "llamar"}]})
        orch = CallOrchestrator(transport=llm.transport())
        res = run(orch.start_or_step_call(req()))
        self.assertTrue(any("Llegó:" in w and "accion" in w for w in res.warnings))


class ApiTests(unittest.TestCase):
    def test_api_409_al_seguir_una_llamada_colgada(self):
        llm = FakeLLM(
            {"assistant_reply": "Buzón.", "tool_calls": [
                {"tool": "log_call_outcome", "arguments": {"outcome": "voicemail_left", "summary": "Mensaje dejado."}},
                {"tool": "hangup_call", "arguments": {"reason": "Buzón"}},
            ]},
        )
        main._orchestrator = CallOrchestrator(transport=llm.transport())
        client = TestClient(main.app)
        body = json.loads(req().model_dump_json())
        r1 = client.post("/api/v1/calls/step", json=body)
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertTrue(r1.json()["terminated"])
        body2 = dict(body, call_id=r1.json()["call_id"], caller_utterance="¿Sigue ahí?")
        r2 = client.post("/api/v1/calls/step", json=body2)
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/api/v1/calendar/events").json(), [])


if __name__ == "__main__":
    unittest.main()
