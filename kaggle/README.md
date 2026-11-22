# Kaggle Fine-Tuning — `vertex-voice-agenda-agent`

Misma receta que vertex-salvador (Gemma 4 E4B, QLoRA con Unsloth). El único cambio de fondo: el modelo responde con
un JSON `{"assistant_reply", "tool_calls"}` dentro de su turno, que es lo que espera el orquestador de la API.

## Archivos

| Archivo | Qué hace |
|---|---|
| `generar_dataset.py` | Crea `data/voice_train.jsonl` (504 llamadas) y `data/voice_val.jsonl` (56). Usa `api/app/policy.py` y el `CalendarBridge` real, así el formato es idéntico al de producción. |
| `validar_dataset.py` | Valida cada tool call con los esquemas de la API y **repite cada llamada en el orquestador real**: los mensajes deben salir iguales carácter por carácter y sin avisos. Si falla, no se sube. |
| `preparar_kaggle.py` | Deja en `kaggle_subir/` solo los 3 archivos del dataset de Kaggle. |
| `entrenar_kaggle.py` | El script de entrenamiento (prueba corta con `--prueba`; la corrida larga entrena, sube el LoRA y el GGUF). |
| `notebooks/entrenar_vertex_voice_agenda.ipynb` | El notebook de 2 celdas (el viejo `train_voice_agenda_qlora.ipynb` está roto y ya no se usa). |
| `prompts/system_call_policy.txt` | Copia de lectura del prompt. La fuente de verdad es `api/app/policy.py`. |

Los archivos viejos `data/train_calls_sft.jsonl` y `data/eval_calls_sft.jsonl` ya no se usan (3 y 1 ejemplos, con
otro prompt). Se pueden borrar.

## Pasos (desde `E:\vertex-voice-agenda-agent`)

```powershell
.\api\venv\Scripts\python.exe kaggle\generar_dataset.py
.\api\venv\Scripts\python.exe kaggle\validar_dataset.py
.\api\venv\Scripts\python.exe kaggle\preparar_kaggle.py
```

Después, en Kaggle:

1. Dataset nuevo con los 3 archivos de `kaggle\kaggle_subir\`.
2. Notebook: GPU T4 x2, Internet On, el dataset como Input y `HF_TOKEN` en Add-ons > Secrets.
3. Las dos celdas del notebook con `--prueba`. Solo si dice **BAJÓ**, quitar `--prueba` y relanzar.
4. Al terminar, el GGUF está en `Denisijcu/vertex-voice-agenda-GGUF` (privado). En LM Studio, el identificador del
   modelo cargado tiene que coincidir con `LLM_DEFAULT_MODEL` del `.env` (o con el campo "Modelo local" de la UI).

Si se cambia `api/app/policy.py`, hay que regenerar el dataset y volver a entrenar.
