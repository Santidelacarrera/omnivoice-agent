"""Persistencia de auditoría, conversaciones, transcripciones, herramientas y consumo.

Dos implementaciones con la misma interfaz: Postgres (producción) y en memoria (desarrollo/tests).
La escritura nunca debe romper la conversación: los errores se registran y se cuentan.
"""
import time
import uuid
from typing import Any, Protocol

import structlog

log = structlog.get_logger()

# Coste estimado de audio por minuto (USD). Ajustar según el contrato con el proveedor.
COST_PER_AUDIO_MINUTE_USD = 0.06


_VERSIONED = ("name", "instructions", "tools", "voice", "language")


class PhoneNumberTaken(Exception):
    """El número ya está registrado (en esta u otra organización: no se revela cuál)."""


def _snapshot(agent: dict[str, Any]) -> dict[str, Any]:
    return {"version": agent["version"], **{k: (list(agent[k]) if k == "tools" else agent[k]) for k in _VERSIONED}}



class Persistence(Protocol):
    async def audit(self, org_id: str | None, actor: str | None, action: str, detail: dict[str, Any]) -> None: ...
    async def start_conversation(self, org_id: str, session_id: str, user_id: str | None) -> str: ...
    async def end_conversation(self, org_id: str, conversation_id: str, summary: dict[str, Any]) -> None: ...
    async def add_transcript(self, org_id: str, conversation_id: str, speaker: str, content: str) -> None: ...
    async def add_event(self, org_id: str, conversation_id: str, type_: str, payload: dict[str, Any], correlation_id: str | None) -> None: ...
    async def record_tool(self, org_id: str, conversation_id: str | None, rec: dict[str, Any]) -> None: ...
    async def list_conversations(self, org_id: str, limit: int = 50) -> list[dict[str, Any]]: ...
    async def get_conversation(self, org_id: str, conversation_id: str) -> dict[str, Any] | None: ...
    async def list_audit(self, org_id: str, limit: int = 200) -> list[dict[str, Any]]: ...
    async def usage_summary(self, org_id: str) -> dict[str, Any]: ...
    async def list_agents(self, org_id: str) -> list[dict[str, Any]]: ...
    async def get_agent(self, org_id: str, agent_id: str) -> dict[str, Any] | None: ...
    async def create_agent(self, org_id: str, name: str, instructions: str, tools: list[str],
                           voice: str | None, language: str) -> dict[str, Any]: ...
    async def update_agent(self, org_id: str, agent_id: str, changes: dict[str, Any]) -> dict[str, Any] | None: ...
    async def list_agent_versions(self, org_id: str, agent_id: str) -> list[dict[str, Any]] | None: ...
    async def rollback_agent(self, org_id: str, agent_id: str, version: int) -> dict[str, Any] | None: ...
    async def set_agent_status(self, org_id: str, agent_id: str, status: str) -> dict[str, Any] | None: ...
    async def list_phone_numbers(self, org_id: str) -> list[dict[str, Any]]: ...
    async def add_phone_number(self, org_id: str, e164: str, agent_id: str | None) -> dict[str, Any]: ...
    async def delete_phone_number(self, org_id: str, phone_id: str) -> bool: ...
    async def org_policy(self, org_id: str) -> dict[str, Any]: ...
    async def set_conversation_options(self, org_id: str, conversation_id: str, *, recording_consent: bool,
                                       voice: str | None, language: str | None) -> None: ...
    async def add_recording(self, org_id: str, conversation_id: str, key: str, size: int, duration_s: float) -> None: ...
    async def get_recording(self, org_id: str, conversation_id: str) -> dict[str, Any] | None: ...
    async def delete_conversation(self, org_id: str, conversation_id: str) -> list[str] | None: ...


class InMemoryPersistence:
    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.agent_versions: dict[str, list[dict[str, Any]]] = {}
        self.phones: dict[str, dict[str, Any]] = {}
        self.audits: list[dict[str, Any]] = []
        self.convs: dict[str, dict[str, Any]] = {}
        self.transcripts: dict[str, list[dict[str, Any]]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.tools: list[dict[str, Any]] = []
        self.recordings: dict[str, list[dict[str, Any]]] = {}
        self.recording_enabled_orgs: set[str] | None = None  # None = todas (desarrollo)

    async def audit(self, org_id, actor, action, detail):
        self.audits.append({"ts": time.time(), "org_id": org_id, "actor": actor, "action": action, "detail": detail})

    async def start_conversation(self, org_id, session_id, user_id):
        cid = uuid.uuid4().hex
        self.convs[cid] = {"id": cid, "org_id": org_id, "session_id": session_id, "user_id": user_id,
                           "created_at": time.time(), "final_state": None}
        return cid

    async def end_conversation(self, org_id, conversation_id, summary):
        c = self.convs.get(conversation_id)
        if c and c["org_id"] == org_id:
            c.update(summary)

    async def add_transcript(self, org_id, conversation_id, speaker, content):
        self.transcripts.setdefault(conversation_id, []).append({"speaker": speaker, "text": content, "ts": time.time()})

    async def add_event(self, org_id, conversation_id, type_, payload, correlation_id):
        self.events.setdefault(conversation_id, []).append({"type": type_, "payload": payload, "correlation_id": correlation_id})

    async def record_tool(self, org_id, conversation_id, rec):
        self.tools.append({"org_id": org_id, "conversation_id": conversation_id, **rec})

    async def list_conversations(self, org_id, limit=50):
        items = [c for c in self.convs.values() if c["org_id"] == org_id]
        return sorted(items, key=lambda c: c["created_at"], reverse=True)[:limit]

    async def get_conversation(self, org_id, conversation_id):
        c = self.convs.get(conversation_id)
        if not c or c["org_id"] != org_id:
            return None
        return {**c, "transcript": self.transcripts.get(conversation_id, []),
                "events": self.events.get(conversation_id, []),
                "tools": [t for t in self.tools if t["conversation_id"] == conversation_id]}

    async def list_audit(self, org_id, limit=200):
        return [a for a in self.audits if a["org_id"] == org_id][-limit:]

    async def usage_summary(self, org_id):
        secs = sum(c.get("audio_seconds", 0) for c in self.convs.values() if c["org_id"] == org_id)
        return {"audio_seconds": secs, "estimated_cost_usd": round(secs / 60 * COST_PER_AUDIO_MINUTE_USD, 4)}

    async def list_agents(self, org_id):
        return [a for a in self.agents.values() if a["org_id"] == org_id]

    async def get_agent(self, org_id, agent_id):
        a = self.agents.get(agent_id)
        return a if a and a["org_id"] == org_id else None

    async def create_agent(self, org_id, name, instructions, tools, voice, language):
        aid = str(uuid.uuid4())
        self.agents[aid] = {"id": aid, "org_id": org_id, "name": name, "instructions": instructions,
                            "tools": tools, "voice": voice, "language": language, "status": "draft", "version": 1}
        self.agent_versions[aid] = [_snapshot(self.agents[aid])]
        return dict(self.agents[aid])

    async def update_agent(self, org_id, agent_id, changes):
        a = await self.get_agent(org_id, agent_id)
        if not a or a["status"] == "archived":
            return None
        a.update(changes)
        a["version"] += 1
        self.agent_versions[agent_id].append(_snapshot(a))
        return dict(a)

    async def list_agent_versions(self, org_id, agent_id):
        if not await self.get_agent(org_id, agent_id):
            return None
        return [dict(v) for v in self.agent_versions[agent_id]]

    async def rollback_agent(self, org_id, agent_id, version):
        old = next((v for v in (await self.list_agent_versions(org_id, agent_id) or []) if v["version"] == version), None)
        if old is None:
            return None
        return await self.update_agent(org_id, agent_id, {k: old[k] for k in _VERSIONED})

    async def set_agent_status(self, org_id, agent_id, status):
        a = await self.get_agent(org_id, agent_id)
        if not a:
            return None
        a["status"] = status
        return dict(a)

    async def list_phone_numbers(self, org_id):
        return [dict(n) for n in self.phones.values() if n["org_id"] == org_id]

    async def add_phone_number(self, org_id, e164, agent_id):
        if any(n["e164"] == e164 for n in self.phones.values()):
            raise PhoneNumberTaken(e164)
        pid = str(uuid.uuid4())
        self.phones[pid] = {"id": pid, "org_id": org_id, "e164": e164, "agent_id": agent_id}
        return dict(self.phones[pid])

    async def delete_phone_number(self, org_id, phone_id):
        n = self.phones.get(phone_id)
        if not n or n["org_id"] != org_id:
            return False
        del self.phones[phone_id]
        return True

    async def org_policy(self, org_id):
        enabled = self.recording_enabled_orgs is None or org_id in self.recording_enabled_orgs
        return {"recording_enabled": enabled}

    async def set_conversation_options(self, org_id, conversation_id, *, recording_consent, voice, language):
        c = self.convs.get(conversation_id)
        if c and c["org_id"] == org_id:
            c.update(recording_consent=recording_consent, voice=voice, language=language,
                     consent_at=time.time() if recording_consent else None)

    async def add_recording(self, org_id, conversation_id, key, size, duration_s):
        if self.convs.get(conversation_id, {}).get("org_id") == org_id:
            self.recordings.setdefault(conversation_id, []).append(
                {"storage_key": key, "bytes": size, "duration_s": duration_s, "created_at": time.time()})

    async def get_recording(self, org_id, conversation_id):
        if self.convs.get(conversation_id, {}).get("org_id") != org_id:
            return None
        recs = self.recordings.get(conversation_id)
        return recs[-1] if recs else None

    async def delete_conversation(self, org_id, conversation_id):
        """Derecho de supresión: borra la conversación y devuelve las claves de grabación a eliminar (None si no existe)."""
        c = self.convs.get(conversation_id)
        if not c or c["org_id"] != org_id:
            return None
        del self.convs[conversation_id]
        self.transcripts.pop(conversation_id, None)
        self.events.pop(conversation_id, None)
        self.tools = [t for t in self.tools if t["conversation_id"] != conversation_id]
        return [r["storage_key"] for r in self.recordings.pop(conversation_id, [])]
