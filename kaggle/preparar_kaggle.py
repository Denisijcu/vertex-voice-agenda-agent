"""
preparar_kaggle.py — Deja en kaggle\\kaggle_subir\\ EXACTAMENTE los 3 archivos que se suben al dataset de Kaggle,
y nada más:
    entrenar_kaggle.py    voice_train.jsonl    voice_val.jsonl

Uso (desde E:\\vertex-voice-agenda-agent):
    .\\api\\venv\\Scripts\\python.exe kaggle\\generar_dataset.py
    .\\api\\venv\\Scripts\\python.exe kaggle\\validar_dataset.py
    .\\api\\venv\\Scripts\\python.exe kaggle\\preparar_kaggle.py
"""
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

KAGGLE = Path(__file__).resolve().parent
ORIGEN, DESTINO, SCRIPT = KAGGLE / "data", KAGGLE / "kaggle_subir", KAGGLE / "entrenar_kaggle.py"
ARCHIVOS = ("voice_train.jsonl", "voice_val.jsonl")

for p in [ORIGEN / n for n in ARCHIVOS] + [SCRIPT]:
    if not p.exists():
        sys.exit(f"No encuentro {p}. ¿Corriste antes generar_dataset.py y validar_dataset.py? No hago nada.")

DESTINO.mkdir(exist_ok=True)
for nombre in ARCHIVOS:
    filas = [json.loads(l) for l in open(ORIGEN / nombre, encoding="utf-8") if l.strip()]
    if not filas:
        sys.exit(f"{nombre} está vacío. No hago nada.")
    shutil.copy2(ORIGEN / nombre, DESTINO / nombre)
    esc = Counter(f["escenario"] for f in filas)
    turnos = sum(1 for f in filas for m in f["messages"] if m["role"] == "assistant")
    print(f"{nombre}: {len(filas)} llamadas, {turnos} turnos del modelo | escenarios: {dict(esc.most_common())}")
shutil.copy2(SCRIPT, DESTINO / SCRIPT.name)
print("=== kaggle_subir listo ===")
for f in sorted(DESTINO.iterdir()):
    print(f"  {f.name:22} {f.stat().st_size:>10} bytes  sha256 {hashlib.sha256(f.read_bytes()).hexdigest()[:8]}")
print("Sube al dataset de Kaggle SOLO estos 3 archivos.")
