# Vertex Voice Agenda Agent

Agente de voz y agenda de **Vertex Coders**: hace llamadas en nombre de una persona (citas dentales o médicas,
contacto con empleadores, seguimiento o campañas), valida cada *tool call* de agenda y desacopla la señalización y
el medio **SIP / RTP** de la inferencia del modelo.

## Cómo funciona un turno

1. La UI manda el turno a `POST /api/v1/calls/step` (primer turno: datos de la llamada; siguientes: lo que dijo la
   contraparte, o un evento como `[evento: buzón de voz] ...`).
2. El orquestador arma el mensaje con `api/app/policy.py` (prompt y formato únicos, los mismos del entrenamiento) y
   llama al modelo local de LM Studio (`http://127.0.0.1:1234/v1`).
3. El modelo contesta `{"assistant_reply": "...", "tool_calls": [...]}` con `check_calendar`, `book_appointment`,
   `log_call_outcome` y `hangup_call`. Cada tool call se valida con Pydantic antes de ejecutarse.

### Reglas que aplica la API, diga lo que diga el modelo

- **Nunca agenda sola.** Una cita solo se reserva si el modelo la pide y cae dentro de los huecos libres consultados
  en esa llamada. Si no, se rechaza y sale un aviso.
- **Dry-run real.** Con `SIP_BRIDGE_ENABLED=false` todo es simulación, aunque la petición diga `dry_run=false`.
- **Llamada colgada = cerrada.** Un turno más sobre una llamada colgada devuelve 409.
- **Fechas siempre con zona horaria.** Una fecha sin zona se interpreta como hora local del PC (sin instalar tzdata).
- **JSON inválido no se lee por teléfono.** Se responde "¿me lo podría repetir?" y se avisa.
- Cada respuesta trae `warnings` con lo que se rechazó o se hizo automáticamente; la UI los muestra.

## Estructura

```text
vertex-voice-agenda-agent/
├── start-agent.ps1            # abre API (8015) y UI (4200) en dos ventanas visibles
├── api/
│   ├── app/
│   │   ├── policy.py          # prompt y formato de mensajes: fuente única (API + dataset)
│   │   ├── call_orchestrator.py
│   │   ├── calendar_bridge.py # agenda en memoria (real + simulada) y validación de huecos
│   │   ├── sip_rtp_bridge.py  # máquina de estados SIP/RTP (simulada por ahora)
│   │   ├── schemas.py · config.py · timeutil.py · main.py
│   ├── tests/test_orchestrator.py   # unittest, sin LM Studio
│   ├── .env.example
│   └── requirements.txt
├── ui/                        # Angular 21 + Material 3: consola con transcripción y avisos
└── kaggle/                    # dataset, validación y entrenamiento (ver kaggle/README.md)
```

## Arranque (desde `E:\vertex-voice-agenda-agent`)

```powershell
# 1. Pruebas del backend (no hace falta LM Studio)
cd api
.\venv\Scripts\python.exe -m unittest discover -s tests -t . -v
cd ..

# 2. Con LM Studio encendido y el modelo cargado
.\start-agent.ps1
```

API: <http://127.0.0.1:8015/docs> · UI: <http://127.0.0.1:4200> · Citas en memoria:
`GET /api/v1/calendar/events`.

## Llamadas de voz reales

`telephony/` añade llamadas de voz de verdad con tu celular (Linphone), sin costo: Asterisk en Docker + pasarela
Python con faster-whisper y Piper que usa esta misma API. Pasos en [`telephony/README.md`](telephony/README.md).

La UI las lanza con **Llamar de verdad** y las sigue en vivo. Endpoints de `api/app/telephony_bridge.py`:

| Endpoint | Quién lo usa | Para qué |
|---|---|---|
| `GET /api/v1/telephony/status` | UI | Asterisk corriendo, celular `Avail`, pasarela viva. |
| `POST /api/v1/telephony/calls` | UI | Guarda la ficha y le pide a Asterisk que marque (`docker exec`, sin shell). 409 si la línea no está lista. |
| `GET /api/v1/telephony/tickets/{id}` | UI | Estado y transcripción en vivo de esa llamada. |
| `GET /api/v1/telephony/calls` | UI | Historial de llamadas reales. |
| `POST /api/v1/telephony/claim` | Pasarela | Al conectar, recibe la ficha y el modelo que eligió la UI. |
| `POST .../calls/{voice_id}/events`, `.../end`, `/heartbeat` | Pasarela | Cada frase, el resultado y "sigo viva". |
| `GET /api/v1/telephony/calls?q=&limit=&offset=` | UI | Historial permanente (más nuevas primero); `q` busca en clínica, resultado y frases. |
| `GET /api/v1/telephony/calls/{voice_id}` | UI | Transcripción completa de una llamada. |

**Historial:** cada llamada real se guarda en `api\data\llamadas.sqlite3` (al conectar, con cada frase y al colgar),
así que sobrevive a los reinicios de la API. Al arrancar, la API importa las transcripciones de `telephony\logs`
que aún no estén. En la UI, la sección **Historial de llamadas** las lista con buscador, abre la transcripción
completa y la descarga en `.txt`. Las respuestas crudas del modelo quedan en `api\logs\llm-AAAAMMDD.jsonl`.
Las dos carpetas están en el `.gitignore`.

## Pendiente

- El puente SIP/RTP de la API sigue siendo la simulación del simulador de texto; las llamadas reales van por `telephony/`.
- La agenda vive en memoria: se pierde al reiniciar la API.
