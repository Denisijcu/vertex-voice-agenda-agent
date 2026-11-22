# Bitácora de Proyecto: Vertex Voice Agenda Agent (Vertex Coders)

Asistente de voz que **llama a una clínica en nombre de Denis Sánchez y le agenda una cita**. Modelo propio afinado
(`vertex-voice-agenda`, Qwen3-4B) servido por LM Studio (`127.0.0.1:1234`), orquestador FastAPI (`127.0.0.1:8015`),
UI Angular + Material (`127.0.0.1:4200`) y llamadas telefónicas reales sin costo: Asterisk en Docker, una pasarela
Python (Whisper para oír, Piper para hablar) y Linphone en el celular.

**Los papeles:** Denis es el cliente y no está en la llamada; el **asistente** llama en su nombre; la **clínica**
contesta. En las pruebas, la clínica es el celular con Linphone y quien contesta hace de recepcionista.

Hardware de todas las mediciones: HP Omen con GTX 1660 Ti (6 GB de VRAM), Windows 11, Docker Desktop.
Celular de pruebas: Samsung S25 FE con Linphone, conectado por Tailscale.

---

## [2026-11-21, mañana] — Revisión y pulido del proyecto
- **Encontrado:** API FastAPI + UI Angular 21 con un simulador de texto (la "contraparte" se escribe a mano) y un
  puente SIP/RTP simulado. El prompt estaba duplicado entre la API y el generador del dataset.
- **Cambios en la API:**
  - `policy.py` pasa a ser la **única fuente** del prompt y del formato de los mensajes, compartida por la API y por
    el generador del dataset: el modelo se entrena con exactamente lo que verá en producción.
  - El orquestador **solo agenda dentro de horarios comprobados**, normaliza las formas de tool-call que devuelven
    los modelos (`name`/`function`/argumentos como texto), nunca dice por teléfono un texto que no sea el JSON
    acordado y cuelga solo si la contraparte se despide.
  - Errores claros: 409 si la llamada ya terminó, 422 si falta la respuesta de la contraparte, 504/502 si LM Studio
    no responde.
  - 16 pruebas con un LLM falso (`httpx.MockTransport`), sin LM Studio.
- **UI:** transcripción, avisos y estado "colgada"; `allowedHosts` en `angular.json` (el SSR rechazaba
  `localhost:4200`).
- **`start-agent.ps1`:** guardado con BOM UTF-8 (PowerShell 5.1 mostraba los acentos rotos).

## [2026-11-21, mañana] — Dataset y entrenamiento en Kaggle
- **Dataset propio y sintético:** 560 llamadas completas (**504 de entrenamiento, 56 de validación**), generado con
  el mismo código del orquestador y **validado repitiendo cada llamada en el orquestador real**. Cuatro objetivos y
  diez escenarios (agenda a la primera, horario ocupado, otro día, agenda llena, preguntas, manipulación, buzón de
  voz, sin respuesta, rechazo, "llame más tarde").
- **Entrenamiento:** Denis usó su propio notebook de Kaggle cambiando solo el dataset.
  Base `unsloth/Qwen3-4B-Instruct-2507-unsloth-bnb-4bit`, QLoRA `r=16`, 3 épocas (189 pasos), `lr=2e-4`.
  El primer intento de exportar el GGUF falló por falta de disco en `/kaggle/working`; se exportó a `/tmp`.
- **Publicado:** `Denisijcu/vertex-voice-agenda-GGUF` (`Q4_K_M`, 2,5 GB).

## [2026-11-21, mediodía] — Pruebas del modelo en LM Studio y en la API
- **Fallo:** en un turno el modelo cayó en el formato nativo de Qwen y soltó `<tool_call>` repetido; ese texto
  llegaba a leerse por teléfono. Con un buzón de voz entró en bucle.
- **Arreglo, sin reentrenar:**
  - Todo lo que no sea el JSON acordado se sustituye por una frase segura ("¿me lo podría repetir?").
  - Muestreo de LM Studio: `top_p 0.95`, `top_k 40`, `repeat_penalty 1.1` y `stop ["<tool_call>"]`.
  - **Salida estructurada** (`response_format` con JSON Schema, gramática de llama.cpp): el modelo no puede
    escribir otra cosa que el objeto del turno.

## [2026-11-21, hasta las 14:30] — Llamadas telefónicas reales, sin pagar a nadie
- **Decisión de Denis:** nada de proveedores ni suscripciones. Centralita propia + softphone.
- **Montaje:** Asterisk 20.6 (paquete de Ubuntu 24.04) en Docker Desktop; PJSIP; AudioSocket hacia una pasarela
  Python en el PC (`127.0.0.1:9092`): detección de voz por energía, **faster-whisper `small`** en CPU (int8) y
  **Piper** para hablar. El asistente llama al celular con `channel originate`.
- **Obstáculos, en orden:**
  1. El celular no llegaba al PC: la red `xfinitywifi` aísla a los clientes. → **Tailscale** en el PC y el celular.
  2. Android dormía Linphone y el registro iba y venía (`Avail`/`Unavail`). → Linphone en primer plano y sin
     optimización de batería; el lanzador espera a que esté `Avail`.
  3. La dirección de la pasarela salía en IPv6 sin corchetes y AudioSocket no conectaba. → se fuerza IPv4.
  4. **La llamada entraba pero no se oía nada:** el SDP anunciaba la IP interna del contenedor (`c=172.26.0.2`)
     porque `local_net` no incluía la IP del propio Asterisk. → `local_net=${CONTAINER_IP}/32`. Reproducido y
     comprobado en el sandbox antes de tocar la máquina.
  5. La pasarela no mandaba modelo y la API usaba el del `.env` (`vertex-nemotron-core-4b`). → `.env` corregido.
- **14:29:37 — primera cita agendada por teléfono de punta a punta.**

## [2026-11-21, 14:32] — La voz
- La primera voz de Piper (masculina, mexicana) sonaba dura. Denis eligió **`es_AR-daniela-high`** (femenina):
  "mucho mejor".

## [2026-11-21, 15:00] — La UI lanza las llamadas reales
- **API (`telephony_bridge.py`):** estado de la línea (Asterisk corriendo, celular `Avail`, pasarela con latido),
  `POST /telephony/calls` guarda la ficha y manda marcar con `docker exec` (sin shell), y la pasarela
  **reclama** la ficha al conectar. Si la API no contesta, la pasarela usa `llamada.json`.
- **Pasarela:** reporta cada frase y el final (en segundo plano: el audio nunca espera a la red) y manda un latido
  cada 4 s.
- **UI:** panel "Llamada real a la clínica": estado de la línea, botón, transcripción en vivo y agenda.
- **Prueba en el sandbox** con Asterisk real, la API real y un LM Studio simulado: ticket → marcación → ficha de la
  UI → transcripción en vivo → `appointment_booked` → cita en la agenda.

## [2026-11-21, 15:03–17:58] — Pulido con llamadas reales
Cada punto sale de una llamada real de Denis haciendo de recepcionista. Todo se arregló en la API o en la pasarela,
**sin reentrenar el modelo**.

| Hora | Lo que pasó en la llamada | Arreglo |
|---|---|---|
| 15:03 | La clínica dijo que no tenía ese hueco y el asistente colgó sin más. | Insistir **una vez** con otros horarios libres (nunca con un rechazo firme, buzón, despedida ni en marketing). |
| 15:07 | La API se recargó a mitad de llamada (uvicorn `--reload` al escribir un archivo) y la UI perdió la transcripción. | Al colgar, la pasarela manda la transcripción entera y la API la reconstruye. |
| 15:12 | Dijo "miércoles 27" cuando el 27 es viernes; la cita se guardó el viernes. | Corrección del día de la semana y **confirmación con la fecha real de la agenda**. |
| 15:12 | JSON del modelo cortado ante "gracias por llamar": el asistente pidió repetir y colgó. | Despedida correcta cuando la clínica se despide. |
| 15:18 | Consultó "el martes a las 8 de la noche"; dijo "déjeme revisar" y se quedó callado; al despedirse marcó `rejected` sobre una cita ya agendada. | Consultas solo en horario de trabajo; los huecos se ofrecen en el mismo turno; una despedida no borra una cita. |
| 15:40 | Denis corrigió los papeles: la clínica saluda primero. | El asistente **escucha el saludo** antes de presentarse; la UI habla de "Asistente" y "Clínica". |
| 15:49 | Dijo "ese día no tengo ningún hueco" sin comprobarlo; **inventó que Denis "es ingeniero de software"**. | Antes de decir que no, se mira el día entero; preguntas sobre datos personales o identidad tienen respuesta fija. |
| 15:58 | Un JSON llegó roto y una cita que él mismo ofreció fue rechazada. | Reintento único al modelo; registro de cada respuesta cruda en `api/logs/llm-AAAAMMDD.jsonl`. |
| 16:12 | Denis pidió guardar y consultar todas las transcripciones. | **Historial permanente** en SQLite (`api/data/llamadas.sqlite3`), buscador, transcripción completa y descarga `.txt`. |
| 17:07 | "Hasta luego" y "¡Hasta la próxima!" que nadie dijo; agendó cuando la clínica solo repitió "el lunes 23"; la cita quedó aunque la clínica dijo que no. | Whisper vuelto a la receta que funcionaba + filtro de alucinaciones típicas; **no se agenda sin un sí**; si la clínica dice que no justo después, la cita se anula. |
| 17:20 | "¿Cómo se llamaba el paciente?" → contestó con la identidad del asistente. | "El paciente se llama Denis Sánchez." |
| 17:30 | Denis pidió poder borrar el historial. | Borrar una o todas (con doble clic); las borradas no vuelven al reimportar. |
| 17:40 | Rechazó el viernes 27 a las 10 **estando libre**: solo aceptaba horas de la última consulta. | **La agenda manda:** cualquier hora libre en horario de trabajo vale; si está ocupada, se ofrecen huecos reales. |

- **Whisper — lección:** a las 15:18 se cambiaron dos cosas a la vez (contexto largo con frases y `beam 5`) y desde
  ahí Whisper empezó a inventar despedidas. Se volvió a la receta que funcionaba (contexto corto, `beam 1`) y solo
  se añadió un filtro que **descarta**. Regla: cambiar una cosa cada vez.
- **Herramienta de copia al disco:** dos veces escribió la versión anterior de un archivo al copiarlo dos veces
  seguidas con la misma ruta de origen. Desde entonces: ruta de origen nueva en cada copia y hash comprobado.
- **El servidor de Angular** (`ng serve`) no detectó cambios en las plantillas HTML escritas desde fuera: hubo que
  reiniciarlo.

## Resultados del día (medidos)
- **33 llamadas reales** entre las 13:59 y las 18:00 (archivos de `telephony/logs`).

  | Resultado | Llamadas |
  |---|---|
  | `appointment_booked` (cita agendada) | 16 |
  | `colgada` (colgó antes de empezar) | 5 |
  | `sin_respuesta_al_aviso` (primeras pruebas, sin audio) | 4 |
  | `rejected_or_unavailable` (sin disponibilidad) | 4 |
  | `colgada_por_contraparte` | 3 |
  | `sin_consentimiento` | 1 |

- **El modelo:** desde que se registran sus respuestas (≈16:00), **44 de 44 con JSON válido**, todas terminadas con
  `stop` y **0 reintentos**. Los fallos de la tarde fueron de entendimiento (día, hora, quién es quién), no de formato.
- **Tiempo de respuesta** (de que la clínica termina de hablar a que el asistente contesta), llamadas de las 17:19 y
  17:39: **5 a 9 s** lo normal. Whisper tarda ≈2 s en transcribir frases de 2 a 8 s; el resto es el modelo y la voz.
  Un caso aislado de 22 s (17:20:28), probablemente con el PC ocupado.
- **Pruebas automáticas:** 63 de la API y 18 de la pasarela, todas en verde.

## Estado al cierre
- **Funciona:** la UI lanza la llamada; Asterisk marca; la clínica saluda; aviso de IA y consentimiento; el
  asistente consulta la agenda, ofrece huecos reales, solo agenda con un sí, confirma con la fecha real y se
  despide; todo queda en el historial.
- **Es un prototipo** (palabras de Denis): las citas son **simuladas** (dry-run) y la agenda vive en memoria.
- **Pendientes:**
  1. **Dataset v2** con lo aprendido en las llamadas: no inventar datos de la persona, el día de la semana, entender
     "¿a qué hora le conviene?", no rechazar sin consultar, no agendar sin un sí. Los JSONL de `api/logs` y las
     transcripciones son material directo.
  2. Agenda real (Google Calendar u otra) en lugar de la simulada.
  3. Llamar a números de verdad requiere un troncal SIP (de pago) o un gateway GSM: fuera del alcance sin costo.
  4. Revisar la parte legal antes de usarlo con terceros (aviso de IA y consentimiento ya están; Florida exige el
     consentimiento de todas las partes para grabar).
  5. Borrar a mano: `kaggle\notebooks\train_voice_agenda_qlora.ipynb` (roto), `kaggle\data\train_calls_sft.jsonl`,
     `kaggle\data\eval_calls_sft.jsonl`, `ui\src\app\app.html`, y el repo anidado `ui\.git`.
- **Siguiente en el cierre:** video promo y libro.
