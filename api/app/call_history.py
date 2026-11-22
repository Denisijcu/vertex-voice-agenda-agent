"""
call_history.py — Historial permanente de las llamadas reales (SQLite, solo biblioteca estándar).

Cada llamada se guarda al conectar, con cada frase y al colgar: sobrevive a reinicios de la API. La UI lo consulta
con búsqueda y abre la transcripción completa.

Al arrancar importa las transcripciones que la pasarela ya dejó en telephony/logs/*.json (las de antes de que
existiera este historial). Lo que ya está no se duplica.
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llamadas (
    voice_id      TEXT PRIMARY KEY,
    ticket_id     TEXT,
    started       TEXT NOT NULL,
    ended         TEXT,
    status        TEXT NOT NULL,
    result        TEXT,
    contact_name  TEXT,
    purpose       TEXT,
    origen        TEXT NOT NULL,          -- ui | pasarela | importada
    turnos        INTEGER NOT NULL DEFAULT 0,
    texto         TEXT NOT NULL DEFAULT '',  -- todas las frases juntas, para buscar
    events        TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS llamadas_started ON llamadas(started DESC);
-- Llamadas borradas a mano: así no vuelven a entrar al importar telephony/logs.
CREATE TABLE IF NOT EXISTS borradas (voice_id TEXT PRIMARY KEY);
"""


class CallHistory:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------ escritura
    def save(self, call: dict[str, Any], origen: str) -> None:
        """Inserta o actualiza una llamada (dict de VoiceCall.to_dict())."""
        events = call.get("events") or []
        texto = "\n".join(f"{e.get('quien', '')}: {e.get('texto', '')}" for e in events)
        ended = call.get("ended") or (
            datetime.now().astimezone().isoformat(timespec="seconds") if call.get("status") == "terminada" else None
        )
        with self._lock:
            self._db.execute(
                """
                INSERT INTO llamadas (voice_id, ticket_id, started, ended, status, result, contact_name, purpose,
                                      origen, turnos, texto, events)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(voice_id) DO UPDATE SET
                    ticket_id = COALESCE(excluded.ticket_id, llamadas.ticket_id),
                    ended = COALESCE(excluded.ended, llamadas.ended),
                    status = excluded.status,
                    result = COALESCE(excluded.result, llamadas.result),
                    contact_name = COALESCE(excluded.contact_name, llamadas.contact_name),
                    purpose = COALESCE(excluded.purpose, llamadas.purpose),
                    turnos = MAX(excluded.turnos, llamadas.turnos),
                    texto = CASE WHEN excluded.turnos >= llamadas.turnos THEN excluded.texto ELSE llamadas.texto END,
                    events = CASE WHEN excluded.turnos >= llamadas.turnos THEN excluded.events ELSE llamadas.events END
                """,
                (
                    call["voice_id"], call.get("ticket_id"), call.get("started") or _ahora(), ended,
                    call.get("status") or "en_curso", call.get("result"), call.get("contact_name"),
                    call.get("purpose"), origen, len(events), texto, json.dumps(events, ensure_ascii=False),
                ),
            )
            self._db.commit()

    def import_gateway_logs(self, logs_dir: Path) -> int:
        """Importa telephony/logs/*.json que aún no estén. Devuelve cuántas entraron."""
        if not logs_dir.is_dir():
            return 0
        with self._lock:
            ya = {r[0] for r in self._db.execute("SELECT voice_id FROM llamadas")}
            ya |= {r[0] for r in self._db.execute("SELECT voice_id FROM borradas")}
        nuevas = 0
        for archivo in sorted(logs_dir.glob("*.json")):
            try:
                data = json.loads(archivo.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            voice_id = data.get("uuid")
            if not voice_id or voice_id in ya:
                continue
            turnos = [
                {"t": _iso_local(t.get("t")), "quien": t.get("quien", ""), "texto": t.get("texto", ""),
                 "tools": t.get("tools", []), "warnings": t.get("warnings", [])}
                for t in data.get("turnos", []) if isinstance(t, dict)
            ]
            inicio = turnos[0]["t"] if turnos else _fecha_de_archivo(archivo)
            self.save(
                {"voice_id": voice_id, "ticket_id": data.get("ticket_id"), "started": inicio,
                 "ended": turnos[-1]["t"] if turnos else inicio, "status": "terminada",
                 "result": data.get("resultado"), "contact_name": data.get("contact_name"),
                 "purpose": data.get("purpose"), "events": turnos},
                origen="importada",
            )
            nuevas += 1
        return nuevas

    def delete(self, voice_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM llamadas WHERE voice_id = ?", (voice_id,))
            if cur.rowcount:
                self._db.execute("INSERT OR IGNORE INTO borradas (voice_id) VALUES (?)", (voice_id,))
            self._db.commit()
            return cur.rowcount > 0

    def delete_all(self, keep: set[str] | None = None) -> int:
        """Borra todo el historial menos las llamadas de 'keep' (las que están en curso)."""
        keep = keep or set()
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT voice_id FROM llamadas") if r[0] not in keep]
            self._db.executemany("DELETE FROM llamadas WHERE voice_id = ?", [(i,) for i in ids])
            self._db.executemany("INSERT OR IGNORE INTO borradas (voice_id) VALUES (?)", [(i,) for i in ids])
            self._db.commit()
        return len(ids)

    # ------------------------------------------------------------------ lectura
    def list(self, limit: int = 20, offset: int = 0, q: str = "") -> dict[str, Any]:
        filtro, params = "", []
        if q.strip():
            filtro = "WHERE contact_name LIKE ? OR result LIKE ? OR texto LIKE ?"
            patron = f"%{q.strip()}%"
            params = [patron, patron, patron]
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM llamadas {filtro}", params).fetchone()[0]
            filas = self._db.execute(
                f"""SELECT voice_id, ticket_id, started, ended, status, result, contact_name, purpose, origen, turnos
                    FROM llamadas {filtro} ORDER BY started DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        return {"total": total, "items": [dict(f) for f in filas]}

    def get(self, voice_id: str) -> dict[str, Any] | None:
        with self._lock:
            fila = self._db.execute("SELECT * FROM llamadas WHERE voice_id = ?", (voice_id,)).fetchone()
        if fila is None:
            return None
        data = dict(fila)
        data["events"] = json.loads(data["events"] or "[]")
        data.pop("texto", None)
        return data


def _ahora() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iso_local(valor: Any) -> str:
    """Las transcripciones viejas guardaban la hora sin zona: se toma como hora local."""
    try:
        return datetime.fromisoformat(str(valor)).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return _ahora()


def _fecha_de_archivo(archivo: Path) -> str:
    try:
        return datetime.strptime(archivo.name[:15], "%Y%m%d-%H%M%S").astimezone().isoformat(timespec="seconds")
    except ValueError:
        return _ahora()
