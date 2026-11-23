# Vertex Voice Agenda Agent — Resumen del proyecto y conclusiones

> **Estado: prototipo.** Funciona de punta a punta con llamadas reales, pero las citas son simuladas (la agenda vive
> en memoria) y todas las pruebas se hicieron contra un softphone propio. No está listo para llamar a terceros.
> Cierre: noviembre de 2026.

## Qué es

Un asistente de voz que **llama a una clínica en nombre de una persona**, negocia la fecha y agenda la cita. Va por
una línea de teléfono de verdad, con un modelo de lenguaje propio corriendo en una PC de casa, y sin pagar ninguna
línea ni servicio de telefonía.

Los papeles:

- **El cliente** (la persona que necesita la cita) **no está en la llamada**.
- **El asistente** llama en su nombre. Dice que es una inteligencia artificial y pide consentimiento.
- **La clínica** contesta y saluda primero.

## Piezas

| Pieza | Qué hace | Dónde |
|---|---|---|
| Modelo `vertex-voice-agenda` | Qwen3-4B afinado con QLoRA en Kaggle sobre 560 llamadas completas, exportado a GGUF `Q4_K_M` | LM Studio, puerto 1234 · Hugging Face `Denisijcu/vertex-voice-agenda-GGUF` |
| Orquestador | Prompt único para entrenar y servir, salida estructurada en JSON, reglas de agenda | `api/` (FastAPI), puerto 8015 |
| Telefonía | Asterisk 20 en Docker (PJSIP + AudioSocket), Linphone en el celular, Tailscale entre los dos | `telephony/` (`vva-asterisk`) |
| Pasarela de voz | Detección de voz, Whisper `small` para oír, Piper `es_AR-daniela-high` para hablar, aviso de IA y consentimiento | `telephony/gateway/`, puerto 9092 |
| Interfaz | Lanza llamadas reales, transcripción en vivo, historial permanente con buscador, descarga y borrado | `ui/` (Angular), puerto 4200 |
| Dataset | Generador y validador que repite cada llamada en el orquestador real | `kaggle/` |

Cómo arrancar cada pieza: ver `README.md` y `telephony/README.md`.

## Resultados del día de pruebas

**33 llamadas reales**, todas registradas en el historial. En todas, la recepcionista fue el propio autor desde su
celular, casi siempre intentando romper al asistente. Es un registro de trabajo, no un benchmark.

| Resultado | Llamadas |
|---|---|
| Cita agendada y confirmada | 16 |
| Colgada antes de empezar | 5 |
| Sin respuesta al aviso (antes de arreglar el audio) | 4 |
| Rechazo aceptado con educación | 4 |
| Colgada por la clínica a mitad | 3 |
| Sin consentimiento (el modelo no llegó a ejecutarse) | 1 |

- **Formato del modelo:** 44 de 44 respuestas fueron JSON válido, sin reintentos.
- **Latencia:** de 5 a 9 s por turno, desde que la clínica deja de hablar hasta que el asistente contesta.
- **Pruebas automáticas:** 63 en la API y 17 en la pasarela, todas en verde. El CI está en `.github/workflows/ci.yml`.

## Conclusiones

1. **El modelo propone y el orquestador decide.** El modelo pequeño sostiene bien la conversación y nunca falló el
   formato, pero no se le puede confiar la agenda: dijo "miércoles 27" para un viernes, agendó sobre una frase que
   era una pregunta y se inventó datos de la persona por la que llamaba. Cada fallo se convirtió en una regla en
   código, no en un reentrenamiento:
   - la agenda dice qué está libre, dentro del horario de trabajo;
   - sin un sí de la clínica no hay cita;
   - un no justo después de agendar cancela la cita;
   - se confirma con la fecha que se guardó, y los días de la semana se corrigen;
   - una despedida no borra una cita;
   - se ofrecen huecos libres en vez de decir "no hay nada" sin mirar;
   - se insiste una vez, con educación;
   - el modelo nunca contesta sobre los datos personales del cliente.

   La lista completa está en `api/app/call_orchestrator.py`, y cada regla tiene su prueba.
2. **Forma resuelta, comprensión no.** Los errores que quedan son de entender: días de la semana, una fecha repetida
   como pregunta, "no, el lunes 23". Eso se arregla con datos, no con un prompt más largo.
3. **Cambiar una cosa a la vez.** El entrenamiento salió bien a la primera siguiendo la receta que ya funcionaba con
   un solo cambio (el dataset). Whisper alucinó despedidas cuando se le cambiaron dos cosas juntas, y se arregló
   volviendo a la receta anterior y añadiendo solo un filtro.
4. **Las transcripciones son la materia prima de la siguiente versión.** El historial de llamadas no es una
   comodidad: cada error que contiene es un ejemplo de entrenamiento en cuanto se corrige.
5. **La línea gratis funciona, con un límite claro.** Asterisk + softphone + Tailscale cuesta cero, pero solo llama a
   teléfonos registrados en la centralita, no a números reales.

## Si se retoma: lo que falta

En este orden:

1. **Dataset v2** a partir de las transcripciones reales y del log del modelo (`api/logs/`). Clínicas que hablan como
   personas, preguntas sobre el paciente, días de la semana escritos por el generador y ningún "no está libre" sin
   consultar antes. Luego una corrida con el mismo notebook de Kaggle, cambiando solo el dataset.
2. **Agenda real** (Google Calendar o similar), y quitar el modo de prueba (*dry run*) solo después de probar cada
   regla contra ella.
3. **Velocidad:** Whisper en streaming y empezar a hablar antes de que el modelo termine.
4. **Interrupciones:** pasar la conversación de semidúplex a full-dúplex.
5. **Números reales:** troncal SIP de pago o pasarela GSM con tarjeta SIM. El resto del sistema no cambia.
6. **Revisión legal** del aviso, el consentimiento, la grabación y la retención de transcripciones antes de llamar a
   cualquiera que no sea el propio autor. Nada de este proyecto es asesoramiento legal.

## Documentación del proyecto

- `logs/bitacora.md` — bitácora completa del desarrollo.
- `README.md` y `telephony/README.md` — instalación y arranque.
- Video promocional en Manim (escena `VertexVoiceAgendaPromo`).
- Libro en dos ediciones: *Let the Calendar Decide* (inglés) y *La agenda manda* (español). El manuscrito no forma
  parte de este repositorio.
