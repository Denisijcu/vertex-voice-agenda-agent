"""
agent_client.py — Cliente de la API del agente. La pasarela no sabe nada del modelo.

- step(): cada turno de la conversación (POST /api/v1/calls/step).
- claim(): al conectar una llamada, pide la ficha (la que lanzó la UI, o la de llamada.json si no hay).
- report_event() / report_end() / heartbeat(): para que la UI siga la llamada en vivo. Nunca bloquean ni tumban
  la llamada: si la API no contesta, solo se pierde el reporte.
"""
import logging

import httpx

log = logging.getLogger("gateway.api")


class AgentError(Exception):
    pass


class AgentClient:
    def __init__(self, api_url: str, model_name: str = "", timeout: float = 60.0) -> None:
        self.api_url = api_url
        self.model_name = model_name
        self.timeout = timeout

    async def _post(self, path: str, body: dict, timeout: float) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(f"{self.api_url}{path}", json=body)

    async def step(self, target: dict, call_id: str | None = None, utterance: str | None = None,
                   model_name: str | None = None) -> dict:
        body = {
            "call_id": call_id,
            "model_name": model_name or self.model_name or None,
            "target": target,
            "caller_utterance": utterance,
            "temperature": 0.2,
            "max_tokens": 384,
        }
        try:
            resp = await self._post("/api/v1/calls/step", body, self.timeout)
        except httpx.HTTPError as exc:
            raise AgentError(f"No pude hablar con la API del agente en {self.api_url}: {exc}") from exc
        if resp.status_code != 200:
            raise AgentError(f"La API respondió {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def claim(self, voice_id: str, fallback_target: dict | None) -> dict:
        """{ticket_id, target, model_name}. Lanza AgentError si la API no contesta."""
        try:
            resp = await self._post("/api/v1/telephony/claim",
                                    {"voice_id": voice_id, "fallback_target": fallback_target}, 5.0)
        except httpx.HTTPError as exc:
            raise AgentError(f"No pude pedir la ficha a la API: {exc}") from exc
        if resp.status_code != 200:
            raise AgentError(f"La API respondió {resp.status_code} al pedir la ficha: {resp.text[:200]}")
        return resp.json()

    async def _report(self, path: str, body: dict) -> None:
        try:
            resp = await self._post(path, body, 3.0)
            if resp.status_code != 200:
                log.debug("Reporte %s rechazado: %s", path, resp.status_code)
        except httpx.HTTPError as exc:
            log.debug("Reporte %s perdido: %s", path, exc)

    async def report_event(self, voice_id: str, quien: str, texto: str, tools=(), warnings=()) -> None:
        await self._report(f"/api/v1/telephony/calls/{voice_id}/events",
                           {"quien": quien, "texto": texto[:2000], "tools": list(tools)[:10],
                            "warnings": [w[:500] for w in list(warnings)[:10]]})

    async def report_end(self, voice_id: str, result: str, resumen: dict | None = None) -> None:
        """Resultado final y, en `resumen`, la ficha y la transcripción entera (para recuperar la llamada en la UI
        si la API se reinició mientras tanto)."""
        body = {"result": result[:64]}
        if resumen:
            body.update({
                "ticket_id": resumen.get("ticket_id"),
                "contact_name": (resumen.get("contact_name") or "")[:200] or None,
                "purpose": (resumen.get("purpose") or "")[:64] or None,
                "events": [{"quien": e["quien"], "texto": e["texto"][:2000], "t": e.get("t"),
                            "tools": list(e.get("tools", []))[:10],
                            "warnings": [w[:500] for w in e.get("warnings", [])][:10]}
                           for e in resumen.get("events", [])[:300]],
            })
        await self._report(f"/api/v1/telephony/calls/{voice_id}/end", body)

    async def heartbeat(self) -> None:
        await self._report("/api/v1/telephony/heartbeat", {})
