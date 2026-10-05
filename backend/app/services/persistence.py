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


class InMemoryPersistence:
    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.audits: list[dict[str, Any]] = []
        self.convs: dict[str, dict[str, Any]] = {}
        self.transcripts: dict[str, list[dict[str, Any]]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.tools: list[dict[str, Any]] = []

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
                            "tools": tools, "voice": voice, "language": language}
        return self.agents[aid]
