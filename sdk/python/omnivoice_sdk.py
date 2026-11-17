"""SDK mínimo de OmniVoice Agent para Python.

    from omnivoice_sdk import OmniVoice
    ov = OmniVoice("https://api.tu-dominio.com", api_key="ovk_...")
    agent = ov.agents.create(name="Recepción", instructions="Atiende llamadas con amabilidad.", tools=["check_inventory"])
    ov.agents.publish(agent["id"])
    job = ov.calls.create(to="+56987654321", consent_basis="existing_customer")

Depende solo de `httpx`. Los errores de la API se convierten en `OmniVoiceError` con el código y el mensaje del servidor.
Una clave de API no puede gestionar claves ni webhooks (la API lo prohíbe): para eso usa un JWT de una persona.
"""
from __future__ import annotations  # las anotaciones `list[...]` no deben resolverse al método `list` de la clase

import hashlib
import hmac
import time
from typing import Any

import httpx

__all__ = ["OmniVoice", "OmniVoiceError", "verify_webhook"]


class OmniVoiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


def verify_webhook(secret: str, signature_header: str, body: str, tolerance_s: int = 300, now: float | None = None) -> bool:
    """Valida `X-OmniVoice-Signature` sobre el cuerpo **crudo**. Falla si la firma no coincide o `t` es más antiguo que la tolerancia."""
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        t, given = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - t) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{t}.{body}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


class _Resource:
    def __init__(self, client: "OmniVoice") -> None:
        self._c = client


class Agents(_Resource):
    def list(self) -> list[dict[str, Any]]:
        return self._c._get("/agents")["items"]

    def get(self, agent_id: str) -> dict[str, Any]:
        return self._c._get(f"/agents/{agent_id}")

    def create(self, name: str, instructions: str, tools: list[str] | None = None, voice: str | None = None,
               language: str = "es") -> dict[str, Any]:
        return self._c._post("/agents", {"name": name, "instructions": instructions, "tools": tools or [], "voice": voice, "language": language})

    def update(self, agent_id: str, **changes: Any) -> dict[str, Any]:
        return self._c._request("PATCH", f"/agents/{agent_id}", json=changes)

    def versions(self, agent_id: str) -> list[dict[str, Any]]:
        return self._c._get(f"/agents/{agent_id}/versions")["items"]

    def rollback(self, agent_id: str, version: int) -> dict[str, Any]:
        return self._c._post(f"/agents/{agent_id}/rollback", {"version": version})

    def publish(self, agent_id: str) -> dict[str, Any]:
        return self._c._post(f"/agents/{agent_id}/publish")

    def archive(self, agent_id: str) -> dict[str, Any]:
        return self._c._post(f"/agents/{agent_id}/archive")

    def simulate(self, agent_id: str, turns: list[str]) -> dict[str, Any]:
        return self._c._post(f"/agents/{agent_id}/simulate", {"turns": turns})


class Calls(_Resource):
    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._c._get("/calls", params={"limit": limit})["items"]

    def get(self, call_id: str) -> dict[str, Any]:
        return self._c._get(f"/calls/{call_id}")

    def create(self, to: str, consent_basis: str, consent_ref: str | None = None, scheduled_at: str | None = None,
               timezone: str | None = None, max_attempts: int | None = None, from_number: str | None = None,
               idempotency_key: str | None = None) -> dict[str, Any]:
        """Programa una llamada saliente. `consent_basis`: explicit_opt_in (exige consent_ref) | existing_customer | transactional."""
        body = {"to": to, "consent_basis": consent_basis, "consent_ref": consent_ref, "scheduled_at": scheduled_at, "timezone": timezone,
                "max_attempts": max_attempts, "from_number": from_number, "idempotency_key": idempotency_key}
        return self._c._post("/calls", {k: v for k, v in body.items() if v is not None})

    def transfer(self, call_id: str, reason: str = "") -> dict[str, Any]:
        return self._c._post(f"/calls/{call_id}/transfer", {"reason": reason})

    def end(self, call_id: str) -> dict[str, Any]:
        return self._c._post(f"/calls/{call_id}/end")


class Outbound(_Resource):
    def list(self) -> list[dict[str, Any]]:
        return self._c._get("/outbound-calls")["items"]

    def get(self, job_id: str) -> dict[str, Any]:
        return self._c._get(f"/outbound-calls/{job_id}")

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self._c._post(f"/outbound-calls/{job_id}/cancel")

    def do_not_call(self, number: str, reason: str | None = None) -> dict[str, Any]:
        return self._c._post("/do-not-call", {"number": number, **({"reason": reason} if reason else {})})


class Webhooks(_Resource):
    def list(self) -> dict[str, Any]:
        return self._c._get("/webhooks")

    def create(self, url: str, events: list[str] | None = None, description: str | None = None) -> dict[str, Any]:
        """Devuelve el secreto de firma **una sola vez**."""
        return self._c._post("/webhooks", {"url": url, **({"events": events} if events else {}), **({"description": description} if description else {})})

    def delete(self, webhook_id: str) -> None:
        self._c._request("DELETE", f"/webhooks/{webhook_id}")

    def deliveries(self, webhook_id: str) -> list[dict[str, Any]]:
        return self._c._get(f"/webhooks/{webhook_id}/deliveries")["items"]


class OmniVoice:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0, http_client: httpx.Client | None = None) -> None:
        self._http = http_client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.agents, self.calls, self.outbound, self.webhooks = Agents(self), Calls(self), Outbound(self), Webhooks(self)

    # ---- núcleo ----
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        r = self._http.request(method, f"/api/v1{path}", headers=self._headers, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise OmniVoiceError(r.status_code, detail if isinstance(detail, str) else str(detail))
        return None if r.status_code == 204 or not r.content else r.json()

    def _get(self, path: str, **kw: Any) -> Any:
        return self._request("GET", path, **kw)

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=body if body is not None else {})

    # ---- consultas ----
    def usage(self, days: int = 30) -> dict[str, Any]:
        return self._get("/usage", params={"days": days})

    def analytics(self, days: int = 30) -> dict[str, Any]:
        return self._get("/analytics", params={"days": days})

    def phone_numbers(self) -> list[dict[str, Any]]:
        return self._get("/phone-numbers")["items"]

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OmniVoice":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
