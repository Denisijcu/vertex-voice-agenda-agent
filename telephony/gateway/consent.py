"""consent.py — ¿La contraparte aceptó que la llamada siga y se transcriba? Reglas fijas, sin modelo."""
import re
import unicodedata

NEGATIVAS_FUERTES = (
    "no estoy de acuerdo", "prefiero que no", "no quiero", "no gracias", "no, gracias", "no acepto",
    "no autorizo", "no me grabe", "no grabe", "no lo acepto", "para nada", "de ninguna manera",
)
AFIRMATIVAS_CON_NO = ("no hay problema", "como no", "no pasa nada", "no importa")
AFIRMATIVAS = (
    "si", "claro", "de acuerdo", "adelante", "vale", "ok", "okay", "por supuesto", "esta bien", "bueno",
    "perfecto", "correcto", "dale", "acepto", "sigamos", "continue", "siga", "digame", "con gusto",
)


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
    return " " + re.sub(r"[^a-z ,]", " ", sin_tildes).replace(",", " , ") + " "


def _contiene(texto: str, frases) -> bool:
    return any(f" {f} " in texto for f in frases)


def clasificar(texto: str) -> str:
    """'si' | 'no' | 'duda'. Ante la duda NO se continúa: el flujo vuelve a preguntar una vez."""
    t = _normalizar(texto)
    if _contiene(t, NEGATIVAS_FUERTES):
        return "no"
    if _contiene(t, AFIRMATIVAS_CON_NO):
        return "si"
    if _contiene(t, AFIRMATIVAS):
        return "si"
    if " no " in t:
        return "no"
    return "duda"
