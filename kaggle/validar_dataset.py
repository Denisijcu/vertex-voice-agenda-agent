"""
validar_dataset.py — Comprueba el dataset ANTES de subirlo a Kaggle. Si algo falla, no se sube.

1. Formato: cada turno del modelo es un JSON {assistant_reply, tool_calls} y cada tool call pasa los esquemas Pydantic
   reales de la API (app/schemas.py).
2. Reproducción: cada llamada se repite en el ORQUESTADOR REAL (app/call_orchestrator.py) con un modelo falso que
   contesta lo que dice el dataset. Se exige que:
     - los mensajes que el orquestador manda al modelo sean idénticos a los del dataset (mismo formato que en
       producción, carácter por carácter);
     - ningún turno genere avisos (ninguna tool call rechazada, ninguna cita fuera de los huecos);
     - la llamada termine colgada.

Uso (desde E:\\vertex-voice-agenda-agent):
    .\\api\\venv\\Scripts\\python.exe kaggle\\validar_dataset.py
"""
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import TypeAdapter, ValidationError

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "api"))

from app import call_orchestrator  # noqa: E402
from app.calendar_bridge import CalendarBridge, CalendarEventRecord  # noqa: E402
from app.config import settings  # noqa: E402
from app.schemas import AgendaToolCall, CallPurpose, StartCallRequest  # noqa: E402

ADAPTER = TypeAdapter(AgendaToolCall)
DATOS = RAIZ / "kaggle" / "data"
AVISOS_INFORMATIVOS = ("check_calendar: no hay huecos libres",)


def comprobar_formato(fila: dict) -> list[str]:
    errores = []
    for i, m in enumerate(fila["messages"]):
        if m["role"] != "assistant":
            continue
        try:
            obj = json.loads(m["content"])
        except json.JSONDecodeError:
            errores.append(f"turno {i}: no es JSON")
            continue
        if set(obj) != {"assistant_reply", "tool_calls"} or not obj["assistant_reply"].strip():
            errores.append(f"turno {i}: claves {sorted(obj)}")
        for tc in obj["tool_calls"]:
            try:
                ADAPTER.validate_python(tc)
            except ValidationError as exc:
                errores.append(f"turno {i}: {tc.get('tool')} inválida ({exc.error_count()} errores)")
    return errores


async def reproducir(fila: dict) -> list[str]:
    meta = fila["meta"]
    mensajes = fila["messages"]
    turnos_modelo = [i for i, m in enumerate(mensajes) if m["role"] == "assistant"]
    errores: list[str] = []
    pendientes = list(turnos_modelo)

    def falso_llm(request: httpx.Request) -> httpx.Response:
        enviado = json.loads(request.content)["messages"]
        idx = pendientes.pop(0)
        esperado = mensajes[:idx]
        if enviado != esperado:
            for j, (a, b) in enumerate(zip(enviado, esperado)):
                if a != b:
                    errores.append(f"turno {idx}: mensaje {j} distinto\n  API:     {a['content'][:300]}\n"
                                   f"  DATASET: {b['content'][:300]}")
                    break
            else:
                errores.append(f"turno {idx}: la API mandó {len(enviado)} mensajes y el dataset tiene {len(esperado)}")
        return httpx.Response(200, json={"choices": [{"message": {"content": mensajes[idx]["content"]}}]})

    cal = CalendarBridge()
    cal._events = [
        CalendarEventRecord(event_id="evt_busy", title="Ocupado", start_iso=datetime.fromisoformat(a),
                            end_iso=datetime.fromisoformat(b), attendee_or_location="-",
                            purpose=CallPurpose.FOLLOW_UP, dry_run=False)
        for a, b in meta["ocupado"]
    ]
    orq = call_orchestrator.CallOrchestrator(calendar_bridge=cal, transport=httpx.MockTransport(falso_llm))
    hoy = datetime.fromisoformat(meta["hoy"])
    call_orchestrator.local_now = lambda: hoy
    settings.principal_name = meta["principal"]

    objetivo = {"contact_name": meta["contacto"], "phone_or_sip_uri": meta["destino"], "purpose": fila["objetivo"],
                "context_notes": meta["notas"], "preferred_window_start": meta["ventana"][0],
                "preferred_window_end": meta["ventana"][1], "dry_run": True}
    res = await orq.start_or_step_call(StartCallRequest(target=objetivo))
    avisos = list(res.warnings)
    for frase in meta["frases"]:
        res = await orq.start_or_step_call(StartCallRequest(call_id=res.call_id, target=objetivo, caller_utterance=frase))
        avisos += res.warnings
    # "no hay huecos" es informativo (escenario de agenda llena); cualquier otro aviso es un error del dataset.
    avisos = [a for a in avisos if not a.startswith(AVISOS_INFORMATIVOS)]
    if avisos:
        errores.append("avisos del orquestador: " + " | ".join(avisos))
    if not res.terminated:
        errores.append("la llamada no terminó colgada")
    if pendientes:
        errores.append(f"quedaron {len(pendientes)} turnos del modelo sin usar")
    return errores


def main() -> None:
    total_mal = 0
    for nombre in ("voice_train.jsonl", "voice_val.jsonl"):
        ruta = DATOS / nombre
        if not ruta.exists():
            sys.exit(f"No encuentro {ruta}. Corre antes kaggle\\generar_dataset.py.")
        filas = [json.loads(l) for l in open(ruta, encoding="utf-8") if l.strip()]
        malas = 0
        resultados = Counter()
        for n, fila in enumerate(filas):
            try:
                errores = comprobar_formato(fila) + asyncio.run(reproducir(fila))
            except Exception as exc:  # una petición que la API rechaza también es un error del dataset
                errores = [f"la API rechazó la llamada: {type(exc).__name__}: {str(exc)[:300]}"]
            if errores:
                malas += 1
                if malas <= 5:
                    print(f"[{nombre} #{n} {fila['escenario']}]\n  " + "\n  ".join(errores))
            ultimo = json.loads(fila["messages"][-1]["content"])["tool_calls"]
            resultados.update(tc["arguments"]["outcome"] for tc in ultimo if tc["tool"] == "log_call_outcome")
        total_mal += malas
        print(f"{nombre}: {len(filas)} llamadas, {malas} con errores | resultados: {dict(resultados)}")
    if total_mal:
        sys.exit(f"\nDATASET NO VÁLIDO: {total_mal} llamadas con errores. No lo subas.")
    print("\nDATASET VÁLIDO: formato correcto y cada llamada se reproduce igual en el orquestador real.")


if __name__ == "__main__":
    main()
