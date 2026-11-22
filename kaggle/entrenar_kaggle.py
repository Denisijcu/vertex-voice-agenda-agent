"""
entrenar_kaggle.py — Afinada de vertex-voice-agenda en Kaggle: QLoRA sobre Gemma 4 E4B (instrucciones) con Unsloth.

Es la receta de vertex-salvador, que funcionó, con UN solo cambio de fondo:
  - Las herramientas NO van con la plantilla nativa: el orquestador de la API espera que el modelo conteste con un
    JSON {"assistant_reply": ..., "tool_calls": [...]} dentro del texto de su turno. Por eso no se declaran tools en
    apply_chat_template y la comprobación de formato solo exige las marcas de turno del modelo.
Todo lo demás es igual: Gemma 4 E4B, tasa 5e-5 constante, sin weight decay, LoRA r=16 alpha=16 solo en capas de
lenguaje, lote 1 × 8, 1 época, longitud 3072, máscara (solo se aprenden los turnos del modelo), sin evaluar ni
guardar a mitad, freno automático y NO se guarda nada si queda peor.

Notebook: GPU T4 x2, Internet On, el dataset añadido como Input, y SOLO estas dos celdas:
    !pip install -q unsloth
    !python "$(find /kaggle/input -name entrenar_kaggle.py | head -1)" --prueba
La prueba corta (40 pasos como máximo) mide la pérdida de validación antes y después. Solo si dice BAJÓ, se cambia la
segunda celda por la misma línea SIN --prueba y se lanza la corrida larga.

La corrida larga lo hace TODO al final, sin otra sesión ni otro Input: guarda el LoRA, lo sube a Hugging Face
(privado, Denisijcu/vertex-voice-agenda), exporta el GGUF q4_k_m y lo sube a Denisijcu/vertex-voice-agenda-GGUF.
El token se lee de Add-ons > Secrets (HF_TOKEN). Para no subir nada: --sin-subir.

--solo-gguf: no entrena; baja el LoRA de Denisijcu/vertex-voice-agenda y vuelve a exportar y subir el GGUF.
"""
import argparse
import glob
import json
import os
import shutil
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--prueba", action="store_true", help="prueba corta: 40 pasos, mide eval_loss antes y después")
ap.add_argument("--solo-gguf", action="store_true", help="no entrena: baja el LoRA de Hugging Face y exporta el GGUF")
ap.add_argument("--subir", default="Denisijcu/vertex-voice-agenda", help="repo privado de Hugging Face para el LoRA")
ap.add_argument("--sin-subir", action="store_true", help="no sube nada a Hugging Face")
ap.add_argument("--epocas", type=float, default=1)
ap.add_argument("--lr", type=float, default=5e-5)
ap.add_argument("--modelo", default="unsloth/gemma-4-E4B-it-unsloth-bnb-4bit")
args = ap.parse_args()

SALIDA = Path("/kaggle/working/salida")
LORA = SALIDA / "lora_vertex_voice_agenda"
MAX_LEN = 3072
PASOS_PRUEBA = 40          # tope; con datasets pequeños la prueba usa el 40 % de una época
HERRAMIENTAS = {"check_calendar", "book_appointment", "log_call_outcome", "hangup_call"}


def obtener_token():
    token = os.environ.get("HF_TOKEN")
    if not token:
        try:
            from kaggle_secrets import UserSecretsClient
            token = UserSecretsClient().get_secret("HF_TOKEN")
        except Exception:
            token = None
    return token


def exportar_gguf_y_subir(model, tokenizer, token):
    """Exporta el GGUF q4_k_m, deja una copia en la salida del notebook y, si hay token, lo sube a <repo>-GGUF."""
    trabajo = Path("/tmp/gguf_trabajo")    # /tmp tiene más espacio que /kaggle/working (20 GB)
    model.save_pretrained_gguf(str(trabajo), tokenizer, quantization_method="q4_k_m")
    # Unsloth deja los .gguf en una carpeta hermana con sufijo "_gguf" (/tmp/gguf_trabajo_gguf), no en la que se le
    # pasa; se buscan en las dos.
    archivos = sorted({f for d in Path("/tmp").glob("gguf_trabajo*") for f in d.rglob("*.gguf")})
    if not archivos:
        sys.exit("La exportación terminó pero no dejó ningún .gguf.")
    (SALIDA / "gguf").mkdir(parents=True, exist_ok=True)
    for f in archivos:
        shutil.copy2(f, SALIDA / "gguf" / f.name)
        print(f"GGUF listo: {SALIDA / 'gguf' / f.name} ({f.stat().st_size / 2**30:.2f} GB)")
    if token and not args.sin_subir:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
        repo_gguf = args.subir + "-GGUF"
        api.create_repo(repo_gguf, private=True, exist_ok=True)
        for f in archivos:
            api.upload_file(path_or_fileobj=str(f), path_in_repo=f.name, repo_id=repo_gguf)
        print(f"GGUF subido (privado) a https://huggingface.co/{repo_gguf}")


# Marcas de la plantilla oficial de Gemma 4 (chat_template.jinja de google/gemma-4-E4B-it).
TURNO_MODELO = "<|turn>model\n"
FIN_TURNO = "<turn|>"
INICIO_RESP_HERR = "<|tool_response>"
FIN_RESP_HERR = "<tool_response|>"


def buscar(nombre):
    hallados = glob.glob(f"/kaggle/input/**/{nombre}", recursive=True) or glob.glob(nombre)
    if not hallados:
        sys.exit(f"No encuentro {nombre} en /kaggle/input. ¿Añadiste el dataset con 'Add Input'?")
    return hallados[0]


def tramos_entrenables(texto):
    """Devuelve los tramos [inicio, fin) del texto que el modelo debe aprender: el contenido de cada turno del
    modelo hasta su <turn|> inclusive. (Aquí no hay respuestas de herramienta nativas, pero se deja la lógica de la
    receta original por si alguna aparece: su cuerpo nunca se aprende.)"""
    tramos = []
    pos = 0
    while True:
        ini = texto.find(TURNO_MODELO, pos)
        if ini < 0:
            break
        ini += len(TURNO_MODELO)
        fin = texto.find(FIN_TURNO, ini)
        fin = len(texto) if fin < 0 else fin + len(FIN_TURNO)
        cursor = ini
        while True:
            r = texto.find(INICIO_RESP_HERR, cursor, fin)
            if r < 0:
                tramos.append((cursor, fin))
                break
            r_fin_marca = r + len(INICIO_RESP_HERR)
            tramos.append((cursor, r_fin_marca))
            cierre = texto.find(FIN_RESP_HERR, r_fin_marca, fin)
            if cierre < 0:
                break
            cursor = cierre + len(FIN_RESP_HERR)
        pos = fin
    return [(a, b) for a, b in tramos if b > a]


def pintar(tok, fila):
    return tok.apply_chat_template(fila["messages"], tokenize=False)


def codificar(tok, fila):
    texto = pintar(tok, fila)
    enc = tok(texto, add_special_tokens=False, return_offsets_mapping=True, truncation=True, max_length=MAX_LEN)
    tramos = tramos_entrenables(texto)
    etiquetas = []
    for tid, (a, b) in zip(enc["input_ids"], enc["offset_mapping"]):
        dentro = any(a >= x and b <= y and b > a for x, y in tramos)
        etiquetas.append(tid if dentro else -100)
    return {"input_ids": enc["input_ids"], "attention_mask": enc["attention_mask"], "labels": etiquetas,
            "largo_total": len(tok(texto, add_special_tokens=False)["input_ids"])}


def mostrar_mascara(tok, fila, limite=3000):
    """Imprime un ejemplo con lo que se aprende entre ⟦ ⟧, para comprobar la máscara a ojo. Se salta el bloque de
    sistema y empieza en el turno del usuario."""
    texto = pintar(tok, fila)
    salida, pos = [], 0
    for a, b in tramos_entrenables(texto):
        salida.append(texto[pos:a])
        salida.append("⟦" + texto[a:b] + "⟧")
        pos = b
    salida.append(texto[pos:])
    completo = "".join(salida)
    inicio = completo.find("<|turn>user")
    print(completo[max(inicio, 0):][:limite])


def revisar_json(texto):
    """Para las pruebas de generación: ¿la salida es el JSON que espera la API y con herramientas permitidas?"""
    limpio = texto.replace("<turn|>", "").replace("<eos>", "").strip()
    try:
        obj = json.loads(limpio)
    except json.JSONDecodeError:
        return "NO es JSON válido"
    if not isinstance(obj, dict) or "assistant_reply" not in obj or not isinstance(obj.get("tool_calls"), list):
        return "JSON sin assistant_reply/tool_calls"
    nombres = [t.get("tool") for t in obj["tool_calls"] if isinstance(t, dict)]
    raras = [n for n in nombres if n not in HERRAMIENTAS]
    return f"JSON válido | herramientas: {nombres}" + (f" | NO PERMITIDAS: {raras}" if raras else "")


from unsloth import FastModel  # noqa: E402  (unsloth tiene que importarse antes que transformers)

# ------------------------------------------------------------ solo GGUF (sesión aparte)
if args.solo_gguf:
    token = obtener_token()
    lora = LORA if LORA.exists() else next(iter(glob.glob("/kaggle/input/**/lora_vertex_voice_agenda", recursive=True)), None)
    origen = str(lora) if lora else args.subir          # si no está en disco, se baja de Hugging Face
    print("Cargo el LoRA desde:", origen)
    model, tokenizer = FastModel.from_pretrained(model_name=origen, max_seq_length=MAX_LEN, load_in_4bit=True,
                                                 token=token)
    exportar_gguf_y_subir(model, tokenizer, token)
    sys.exit(0)

# ------------------------------------------------------------ afinada
import torch  # noqa: E402
from datasets import Dataset  # noqa: E402
from transformers import Trainer, TrainerCallback, TrainingArguments  # noqa: E402

model, tokenizer = FastModel.from_pretrained(model_name=args.modelo, max_seq_length=MAX_LEN, load_in_4bit=True)
model = FastModel.get_peft_model(
    model,
    finetune_vision_layers=False, finetune_language_layers=True,
    finetune_attention_modules=True, finetune_mlp_modules=True,
    r=16, lora_alpha=16, lora_dropout=0, bias="none", random_state=42,
    use_gradient_checkpointing="unsloth",
)
tok = getattr(tokenizer, "tokenizer", tokenizer)   # el tokenizador de texto dentro del procesador multimodal
if tok.pad_token_id is None:
    tok.pad_token = tok.eos_token

filas_train = [json.loads(l) for l in open(buscar("voice_train.jsonl"), encoding="utf-8") if l.strip()]
filas_val = [json.loads(l) for l in open(buscar("voice_val.jsonl"), encoding="utf-8") if l.strip()]

# Comprobación de formato antes de gastar GPU: la plantilla debe usar las marcas que espera la máscara.
muestra = next(f for f in filas_train if f["escenario"] == "ocupado")
texto_muestra = pintar(tok, muestra)
for marca in (TURNO_MODELO, FIN_TURNO):
    if marca not in texto_muestra:
        sys.exit(f"La plantilla del modelo no produce la marca {marca!r}. No entreno: la máscara no sería fiable.")
print("=" * 80 + "\nEJEMPLO DE LLAMADA (lo que se aprende va entre ⟦ ⟧):\n")
mostrar_mascara(tok, muestra)
print("=" * 80)

datos_train = [codificar(tok, f) for f in filas_train]
datos_val = [codificar(tok, f) for f in filas_val]
largos = [d["largo_total"] for d in datos_train]
cortados = sum(l > MAX_LEN for l in largos)
sin_etiquetas = sum(all(x == -100 for x in d["labels"]) for d in datos_train)
print(f"Ejemplos: train {len(datos_train)}, val {len(datos_val)} | tokens: máx {max(largos)}, medio "
      f"{sum(largos) // len(largos)} | cortados a {MAX_LEN}: {cortados} | sin nada que aprender: {sin_etiquetas}")
if sin_etiquetas:
    sys.exit("Hay ejemplos sin tokens que aprender: la máscara falló. No entreno.")
import math  # noqa: E402
pasos_epoca = math.ceil(len(datos_train) / 8)
pasos_prueba = min(PASOS_PRUEBA, max(5, round(0.4 * pasos_epoca)))
print(f"Pasos por época: {pasos_epoca} | pasos de la prueba corta: {pasos_prueba}")
if pasos_epoca * args.epocas < 40:
    print(f"AVISO: la corrida larga tendrá solo {math.ceil(pasos_epoca * args.epocas)} pasos. La receta del exilio "
          "se hizo con ~60 pasos por época; con tan pocos ejemplos el modelo cambiará poco.")
ds_train = Dataset.from_list([{k: d[k] for k in ("input_ids", "attention_mask", "labels")} for d in datos_train])
ds_val = Dataset.from_list([{k: d[k] for k in ("input_ids", "attention_mask", "labels")} for d in datos_val])


def juntar(lote):
    largo = max(len(x["input_ids"]) for x in lote)
    pad = tok.pad_token_id
    return {
        "input_ids": torch.tensor([x["input_ids"] + [pad] * (largo - len(x["input_ids"])) for x in lote]),
        "attention_mask": torch.tensor([x["attention_mask"] + [0] * (largo - len(x["attention_mask"])) for x in lote]),
        "labels": torch.tensor([x["labels"] + [-100] * (largo - len(x["labels"])) for x in lote]),
    }


cfg = TrainingArguments(
    per_device_train_batch_size=1,
    gradient_accumulation_steps=8,
    per_device_eval_batch_size=1,
    num_train_epochs=args.epocas,
    max_steps=pasos_prueba if args.prueba else -1,
    learning_rate=args.lr,
    lr_scheduler_type="constant",
    warmup_steps=0,
    weight_decay=0.0,
    optim="adamw_8bit",
    fp16=True, bf16=False,
    logging_steps=5,
    eval_strategy="no",
    save_strategy="no",
    output_dir="/tmp/checkpoints",
    seed=42,
    report_to="none",
    remove_unused_columns=False,
)


class Freno(TrainerCallback):
    """Si la pérdida media de los últimos 5 registros supera en 0,3 a la de los 5 primeros, para."""
    def __init__(self):
        self.perdidas, self.frenado = [], False

    def on_log(self, args_, state, control, logs=None, **kw):
        if logs and "loss" in logs:
            self.perdidas.append(float(logs["loss"]))
            if len(self.perdidas) >= 10:
                ini, fin = sum(self.perdidas[:5]) / 5, sum(self.perdidas[-5:]) / 5
                if fin > ini + 0.3:
                    print(f"\nFRENO: la pérdida subió de {ini:.3f} a {fin:.3f}. Paro el entrenamiento.")
                    control.should_training_stop = True
                    self.frenado = True


freno = Freno()
trainer = Trainer(model=model, args=cfg, train_dataset=ds_train, eval_dataset=ds_val, data_collator=juntar,
                  callbacks=[freno])

antes = trainer.evaluate()["eval_loss"]
print(f"\neval_loss ANTES de entrenar: {antes:.4f}")
trainer.train()
print("Pérdida de entrenamiento registrada:", [round(x, 3) for x in freno.perdidas])
despues = trainer.evaluate()["eval_loss"]
veredicto = "BAJÓ: aprendió" if despues < antes and not freno.frenado else "SUBIÓ: NO sirve, no lo uses"
print(f"\neval_loss ANTES: {antes:.4f} | DESPUÉS: {despues:.4f} | {veredicto}")

if args.prueba:
    print(f"\nPRUEBA CORTA ({pasos_prueba} pasos) terminada. Si dice BAJÓ, lanza la corrida larga sin --prueba. "
          "No se guarda nada en la prueba.")
elif freno.frenado or despues >= antes:
    sys.exit("No guardo el LoRA: el modelo quedó peor que el original.")
else:
    SALIDA.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(LORA))
    tokenizer.save_pretrained(str(LORA))
    (SALIDA / "historial.json").write_text(json.dumps(trainer.state.log_history, indent=2), encoding="utf-8")
    print(f"Guardado el LoRA en {LORA}")

# ---- 5 pruebas de generación sobre validación: 3 aperturas de llamada (debe saludar y llamar a check_calendar con
# la ventana) y 2 cortes a mitad de llamada (después de una respuesta de la contraparte con huecos). Se imprime en
# crudo y se dice si el JSON es el que espera la API.
FastModel.for_inference(model)
pruebas = []
for f in filas_val[:3]:
    pruebas.append(f["messages"][:2])
for f in [f for f in filas_val if f["escenario"] in ("ocupado", "acepta_hueco")][:2]:
    corte = next(i for i, m in enumerate(f["messages"]) if i > 2 and m["role"] == "user")
    pruebas.append(f["messages"][:corte + 1])
for mensajes in pruebas:
    texto = tok.apply_chat_template(mensajes, tokenize=False, add_generation_prompt=True)
    entrada = tok(texto, add_special_tokens=False, return_tensors="pt").to("cuda")
    torch.manual_seed(42)
    salida = model.generate(**entrada, max_new_tokens=400, do_sample=True, temperature=0.3, top_p=0.95, top_k=64,
                            repetition_penalty=1.05)
    crudo = tok.decode(salida[0, entrada["input_ids"].shape[1]:], skip_special_tokens=False).strip()
    print("\n" + "=" * 80 + "\nÚLTIMO MENSAJE:", mensajes[-1]["content"][:400])
    print("SALIDA EN CRUDO:", crudo)
    print("REVISIÓN:", revisar_json(crudo))

# ---- Final de la corrida larga: subir el LoRA, exportar el GGUF y subirlo. Va después de las pruebas para que un
# fallo aquí no se lleve por delante lo anterior; el LoRA ya está guardado en la salida del notebook.
if not args.prueba:
    token = obtener_token()
    if args.sin_subir:
        print("\n--sin-subir: no subo nada a Hugging Face.")
    elif not token:
        print("\nAVISO: no hay HF_TOKEN en Secrets; no subo nada (el LoRA y el GGUF quedan en la salida del notebook).")
    else:
        model.push_to_hub(args.subir, token=token, private=True)
        tokenizer.push_to_hub(args.subir, token=token, private=True)
        print(f"\nLoRA subido (privado) a https://huggingface.co/{args.subir}")
    print("\nExporto el GGUF q4_k_m (tarda unos minutos)...")
    exportar_gguf_y_subir(model, tokenizer, token)
    print("\nTERMINADO. Para LM Studio, baja el .gguf de https://huggingface.co/" + args.subir + "-GGUF"
          " o de la pestaña Output, carpeta salida/gguf.")
