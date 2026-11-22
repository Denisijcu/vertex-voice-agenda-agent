# Telefonía real — Vertex Voice Agenda Agent

Llamadas de voz **reales** entre el agente y tu celular, sin operador ni suscripciones: una centralita Asterisk
local (Docker), Linphone en el celular, y una pasarela Python que escucha (faster-whisper), habla (Piper) y le
pasa cada turno a la **misma API del agente** (puerto 8015). El modelo, el orquestador y la agenda no cambian.

```text
Linphone (celular) ⇄ Asterisk (Docker, SIP/RTP) ⇄ AudioSocket ⇄ pasarela Python (gateway/)
                                                        ├─ faster-whisper: voz → texto
                                                        ├─ API del agente (8015) → LM Studio
                                                        └─ Piper: assistant_reply → voz
```

## Cómo va una llamada

1. **Aviso fijo** (no lo dice el modelo): *"Hola, le habla un asistente de inteligencia artificial que llama en
   nombre de Denis Sánchez. Esta llamada se transcribe para tomar nota. ¿Está de acuerdo en continuar?"*
2. **Sí** → empieza el modelo con su saludo normal. **No**, silencio, o duda dos veces → despedida fija y cuelga;
   el modelo ni se entera.
3. Turnos: la pasarela detecta cuándo terminas de hablar, transcribe, llama a la API y lee la respuesta.
4. Si la API **rechaza una cita**, por teléfono NO se lee la confirmación del modelo: se dice *"Perdone, ese
   horario no lo tengo libre en la agenda. ¿Tendría otro horario disponible?"*.
5. Cuelga cuando el agente cuelga (`hangup_call`), cuando cuelgas tú, o tras dos silencios largos.
6. La transcripción de cada llamada queda en `telephony\logs\` (fuera del repositorio).

Conversación por turnos: mientras el agente habla, lo que digas no se escucha. Espera a que termine.

## Instalación (una sola vez)

Todo desde `E:\vertex-voice-agenda-agent\telephony` salvo que se diga otra cosa.

1. **Linphone** en el celular (gratis, App Store / Play Store). Al crear la cuenta, elige *usar cuenta SIP*.
2. **Configuración:**
   ```powershell
   copy .env.example .env
   notepad .env
   ```
   Cambia `SIP_PASSWORD`. En `HOST_IP` pon la IP de tu PC que ve el celular: **172.20.20.20** si el celular está
   en tu mismo Wi-Fi, o **100.95.58.114** si el celular tiene Tailscale (así funciona hasta fuera de casa).
3. **Firewall** (tu Wi-Fi está marcado como red Pública). PowerShell **como administrador**, en esta carpeta:
   ```powershell
   .\abrir-firewall.ps1
   ```
   Abre solo SIP 5060/udp y RTP 10000-10020/udp, y solo para tu red local y Tailscale.
4. **Entorno Python propio con 3.12** (no toca el venv de la API ni los paquetes globales):
   ```powershell
   D:\python312\python.exe -m venv venv
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
5. **Voz en español** (63 MB):
   ```powershell
   .\venv\Scripts\python.exe -m piper.download_voices es_MX-ald-medium --download-dir models\piper
   ```
6. **Pruebas** (sin Asterisk ni modelos; deben salir 11 OK):
   ```powershell
   .\venv\Scripts\python.exe -m unittest discover -s tests -t . -v
   ```

## Cada vez que quieras llamar

Antes: LM Studio con el modelo cargado y la API levantada (`.\start-agent.ps1` en la raíz del proyecto).

1. Abre **Docker Desktop** y, en una ventana en `telephony`:
   ```powershell
   docker compose up --build
   ```
   Debe salir `[vva] HOST_IP=...` y Asterisk arrancando. La primera vez tarda unos minutos (baja Ubuntu e instala
   Asterisk).
2. En otra ventana en `telephony`, la pasarela:
   ```powershell
   .\venv\Scripts\python.exe -m gateway
   ```
   La primera vez baja Whisper `small` (483 MB) a `models\whisper`. Listo cuando dice `Pasarela escuchando`.
3. En **Linphone**: usuario `denis` (o tu `SIP_USER`), contraseña la de `SIP_PASSWORD`, dominio = tu `HOST_IP`,
   transporte **UDP**. Comprueba que se registró:
   ```powershell
   docker exec vva-asterisk asterisk -rx "pjsip show contacts"
   ```
4. Llama:
   - **Desde la UI (lo normal):** en <http://127.0.0.1:4200>, el panel **Llamada real a la clínica** muestra si
     Asterisk, el teléfono de la clínica y la pasarela están listos. Rellena la ficha y pulsa **Llamar a la clínica**.

     Los papeles: **Denis** es el cliente (no está en la llamada); el **asistente** llama en su nombre; la
     **clínica** contesta. En pruebas la clínica es el celular con Linphone: contesta como recepcionista y saluda
     primero ("Clínica Dental Dr. Ramírez, ¿en qué puedo ayudarle?"). El asistente espera a que termine el saludo
     (hasta 6 s; si nadie habla, arranca igual) y luego se presenta y pide consentimiento. La conversación sale en vivo en la UI, con las herramientas
     que usa el agente, los avisos y el resultado; abajo, las últimas llamadas y la agenda.
   - **Tú llamas al agente:** marca `100` en Linphone. Usa la ficha de `llamada.json`.
   - **Sin UI:** `.\llamar.ps1`. También usa `llamada.json`.

Cuando la llamada sale de la UI, la pasarela le pide la ficha y el modelo a la API al conectar; si la API no
contesta, usa `llamada.json` y la llamada sigue igual. `llamada.json` se relee en cada llamada.

## Si algo falla

| Síntoma | Causa probable |
|---|---|
| Linphone no se registra | `HOST_IP` equivocada, firewall sin abrir, o Docker Desktop apagado. |
| Hay llamada pero no se oye nada (o solo en un sentido) | `HOST_IP` no es la que ve el celular, o falta el rango RTP 10000-10020 en el firewall. |
| El agente no reacciona cuando hablas | Sube el volumen o baja `VAD_UMBRAL` en `.env` (p. ej. 400). |
| Te corta antes de terminar la frase | Sube `VAD_SILENCIO_MS` (p. ej. 1200). |
| Tarda mucho en contestar | `WHISPER_MODEL=base` (más rápido, algo menos preciso). |
| "No pude hablar con la API del agente" | La API no está en el 8015: `.\start-agent.ps1` en la raíz. |
| La UI dice "Celular dormido" o "sin registrar" | Abre Linphone en el celular y deja la pantalla encendida (Android lo duerme). |
| La UI dice "Pasarela de voz" en rojo | La pasarela no corre o se arrancó antes que la API: arráncala (paso 2); se reconecta sola. |
| La UI dice que Asterisk no corre pero sí corre | La API no encuentra `docker`: pon la ruta completa en `TELEPHONY_DOCKER_BIN` del `api\.env`. |
| "No contestaron" en la UI | Pasaron 90 s sin que contestaras (`TELEPHONY_TICKET_TTL_SEC`). |

En el log de Asterisk, al final de cada llamada sale `Failed to receive frame from AudioSocket`: es normal, es
Asterisk enterándose de que la pasarela colgó.

## Lo que hay aquí

| Archivo | Qué es |
|---|---|
| `docker-compose.yml`, `asterisk/` | Centralita: Asterisk 20 del paquete oficial de Ubuntu 24.04 (con AudioSocket), plantillas de configuración y `entrypoint.sh`. `chan_sip` va desactivado para que no pelee por el 5060. |
| `gateway/` | Pasarela: protocolo AudioSocket, detección de voz, Whisper, Piper, consentimiento y cliente de la API. |
| `tests/` | Pruebas con un Asterisk falso por TCP: flujo completo, consentimiento, silencios, cuelgue y cita rechazada. |
| `llamada.json` | Ficha para las llamadas que no salen de la UI (marcar 100 o `llamar.ps1`). |
| `llamar.ps1`, `abrir-firewall.ps1` | El agente te llama; abrir los puertos. |

Probado de punta a punta con Asterisk 20.6 real (el mismo de la imagen), la API real y un LM Studio simulado:
aviso, consentimiento, `check_calendar`, `book_appointment`, cita rechazada corregida por voz, cierre y
transcripción. También la llamada lanzada desde la UI: `POST /api/v1/telephony/calls` → Asterisk marca → la
pasarela reclama la ficha de la UI → la transcripción en vivo → `appointment_booked` en el historial y la cita en
la agenda. Lo que no se pudo probar fuera de tu PC: Docker Desktop con tu red y Linphone.

## Llamadas a números reales

Para llamar a una clínica de verdad hace falta un operador SIP con número (cuesta por minuto), y respetar la
TCPA (consentimiento para llamadas con voz de IA) y la ley de Florida de consentimiento de todas las partes para
transcribir. El aviso inicial ya está pensado para eso, pero antes de llamar a terceros, revísalo con un abogado.
