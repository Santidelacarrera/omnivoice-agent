"""Persistencia de auditoría, conversaciones, transcripciones, herramientas y consumo.

Dos implementaciones con la misma interfaz: Postgres (producción) y en memoria (desarrollo/tests).
La escritura nunca debe romper la conversación: los errores se registran y se cuentan.
"""
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import structlog

from app.telephony.call_state import ANSWERED, CONNECTED, CREATED, can_transition, is_terminal

log = structlog.get_logger()

# Coste estimado de audio por minuto (USD). Ajustar según el contrato con el proveedor.
COST_PER_AUDIO_MINUTE_USD = 0.06


_VERSIONED = ("name", "instructions", "tools", "voice", "language")


ACTIVE_JOB_STATES = ("SCHEDULED", "DIALING")


class OutboundRejected(Exception):
    """El trabajo saliente no se crea: `reason` = do_not_call | duplicate_active."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    async def add_phone_number(self, org_id: str, e164: str, agent_id: str | None,
                               transfer_number: str | None = None) -> dict[str, Any]: ...
    async def update_phone_number(self, org_id: str, phone_id: str, changes: dict[str, Any]) -> dict[str, Any] | None: ...
    async def get_call_by_sid(self, org_id: str, provider: str, sid: str) -> dict[str, Any] | None: ...
    async def delete_phone_number(self, org_id: str, phone_id: str) -> bool: ...
    async def resolve_phone_number(self, e164: str) -> dict[str, Any] | None: ...
    async def upsert_call(self, org_id: str, provider: str, sid: str, direction: str, agent_id: str | None,
                          our_number: str | None, peer_hash: str | None) -> dict[str, Any] | None: ...
    async def apply_call_event(self, org_id: str, provider: str, sid: str, status: str, event_key: str,
                               detail: dict[str, Any]) -> dict[str, Any] | None: ...
    async def resolve_call_org(self, provider: str, sid: str) -> str | None: ...
    async def list_calls(self, org_id: str, limit: int = 50) -> list[dict[str, Any]]: ...
    async def get_call(self, org_id: str, call_id: str) -> dict[str, Any] | None: ...
    async def create_outbound_job(self, org_id: str, **fields: Any) -> dict[str, Any]: ...
    async def get_outbound_job(self, org_id: str, job_id: str) -> dict[str, Any] | None: ...
    async def list_outbound_jobs(self, org_id: str, limit: int = 50) -> list[dict[str, Any]]: ...
    async def cancel_outbound_job(self, org_id: str, job_id: str) -> dict[str, Any] | None: ...
    async def claim_due_outbound_jobs(self, now: datetime, limit: int, stale_s: int) -> list[dict[str, Any]]: ...
    async def claim_outbound_job(self, org_id: str, job_id: str, now: datetime) -> dict[str, Any] | None: ...
    async def mark_outbound_dialed(self, org_id: str, job_id: str, call_sid: str) -> None: ...
    async def reschedule_outbound_job(self, org_id: str, job_id: str, when: datetime) -> None: ...
    async def finish_outbound_attempt(self, org_id: str, *, call_status: str, now: datetime,
                                      retry_delay: Callable[[int], timedelta], call_sid: str | None = None,
                                      job_id: str | None = None) -> dict[str, Any] | None: ...
    async def add_dnc(self, org_id: str, number_hash: str, reason: str | None, by: str | None) -> dict[str, Any]: ...
    async def list_dnc(self, org_id: str) -> list[dict[str, Any]]: ...
    async def is_do_not_call(self, org_id: str, number_hash: str) -> bool: ...
    async def remove_dnc(self, org_id: str, dnc_id: str) -> bool: ...
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
        self.calls: dict[str, dict[str, Any]] = {}
        self.call_events: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.dnc: dict[str, dict[str, Any]] = {}
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
        return [self._public_phone(n) for n in self.phones.values() if n["org_id"] == org_id]

    async def add_phone_number(self, org_id, e164, agent_id, transfer_number=None):
        if any(n["e164"] == e164 for n in self.phones.values()):
            raise PhoneNumberTaken(e164)
        pid = str(uuid.uuid4())
        self.phones[pid] = {"id": pid, "org_id": org_id, "e164": e164, "agent_id": agent_id, "transfer_number": transfer_number}
        return self._public_phone(self.phones[pid])

    @staticmethod
    def _public_phone(n):
        return {"id": n["id"], "e164": n["e164"], "agent_id": n["agent_id"], "transfer_number": n.get("transfer_number")}

    async def update_phone_number(self, org_id, phone_id, changes):
        n = self.phones.get(phone_id)
        if not n or n["org_id"] != org_id:
            return None
        n.update(changes)
        return self._public_phone(n)

    async def get_call_by_sid(self, org_id, provider, sid):
        c = next((c for c in self.calls.values() if c["org_id"] == org_id and c["provider"] == provider and c["sid"] == sid), None)
        return self._public_call(c) if c else None

    async def upsert_call(self, org_id, provider, sid, direction, agent_id, our_number, peer_hash):
        existing = next((c for c in self.calls.values() if c["provider"] == provider and c["sid"] == sid), None)
        if existing:
            return {"id": existing["id"], "status": existing["status"], "created": False}
        cid = str(uuid.uuid4())
        self.calls[cid] = {"id": cid, "org_id": org_id, "provider": provider, "sid": sid, "direction": direction,
                           "agent_id": agent_id, "our_number": our_number, "peer_hash": peer_hash, "status": CREATED,
                           "created_at": time.time(), "answered_at": None, "ended_at": None}
        self.call_events.append({"call_id": cid, "org_id": org_id, "status": CREATED, "event_key": "created",
                                 "applied": True, "detail": {}, "ts": time.time()})
        return {"id": cid, "status": CREATED, "created": True}

    async def apply_call_event(self, org_id, provider, sid, status, event_key, detail):
        call = next((c for c in self.calls.values() if c["provider"] == provider and c["sid"] == sid and c["org_id"] == org_id), None)
        if call is None:
            return None
        if any(e["call_id"] == call["id"] and e["event_key"] == event_key for e in self.call_events):
            return {"call_id": call["id"], "status": call["status"], "applied": False, "duplicate": True}
        applied = can_transition(call["status"], status)
        if applied:
            call["status"] = status
            now = time.time()
            if status in (ANSWERED, CONNECTED) and call["answered_at"] is None:
                call["answered_at"] = now
            if is_terminal(status):
                call["ended_at"] = now
        self.call_events.append({"call_id": call["id"], "org_id": org_id, "status": status, "event_key": event_key,
                                 "applied": applied, "detail": dict(detail), "ts": time.time()})
        return {"call_id": call["id"], "status": call["status"], "applied": applied, "duplicate": False}

    async def resolve_call_org(self, provider, sid):
        c = next((c for c in self.calls.values() if c["provider"] == provider and c["sid"] == sid), None)
        return c["org_id"] if c else None

    @staticmethod
    def _public_call(c):
        return {k: v for k, v in c.items() if k != "org_id"}

    async def list_calls(self, org_id, limit=50):
        rows = sorted((c for c in self.calls.values() if c["org_id"] == org_id), key=lambda c: c["created_at"], reverse=True)
        return [self._public_call(c) for c in rows[:limit]]

    async def get_call(self, org_id, call_id):
        c = self.calls.get(call_id)
        if not c or c["org_id"] != org_id:
            return None
        events = [{k: v for k, v in e.items() if k not in ("call_id", "org_id")} for e in self.call_events if e["call_id"] == call_id]
        return {**self._public_call(c), "events": events}

    # ---- llamadas salientes ----
    async def create_outbound_job(self, org_id, **f):
        if any(d["org_id"] == org_id and d["number_hash"] == f["number_hash"] for d in self.dnc.values()):
            raise OutboundRejected("do_not_call")
        key = f.get("idempotency_key")
        if key:
            old = next((j for j in self.jobs.values() if j["org_id"] == org_id and j["idempotency_key"] == key), None)
            if old:
                return {"job": dict(old), "created": False}
        if any(j["org_id"] == org_id and j["number_hash"] == f["number_hash"] and j["status"] in ACTIVE_JOB_STATES
               for j in self.jobs.values()):
            raise OutboundRejected("duplicate_active")
        jid = str(uuid.uuid4())
        self.jobs[jid] = {"id": jid, "org_id": org_id, "status": "SCHEDULED", "attempts": 0, "claimed_at": None,
                          "last_call_sid": None, "last_result": None, "created_at": time.time(), "finished_at": None, **f}
        return {"job": dict(self.jobs[jid]), "created": True}

    async def get_outbound_job(self, org_id, job_id):
        j = self.jobs.get(job_id)
        return dict(j) if j and j["org_id"] == org_id else None

    async def list_outbound_jobs(self, org_id, limit=50):
        rows = sorted((j for j in self.jobs.values() if j["org_id"] == org_id), key=lambda j: j["created_at"], reverse=True)
        return [dict(j) for j in rows[:limit]]

    @staticmethod
    def _finish(j, status, now):
        j["status"], j["finished_at"], j["to_number"], j["claimed_at"] = status, now.timestamp(), None, None

    async def cancel_outbound_job(self, org_id, job_id):
        j = self.jobs.get(job_id)
        if not j or j["org_id"] != org_id:
            return None
        if j["status"] == "SCHEDULED":
            self._finish(j, "CANCELLED", datetime.now(timezone.utc))
            j["last_result"] = "cancelled_by_user"
        return dict(j)

    async def claim_due_outbound_jobs(self, now, limit, stale_s):
        due = [j for j in self.jobs.values()
               if (j["status"] == "SCHEDULED" and j["next_attempt_at"] <= now)
               or (j["status"] == "DIALING" and j["claimed_at"] and j["claimed_at"] < now - timedelta(seconds=stale_s))]
        due.sort(key=lambda j: j["next_attempt_at"])
        out = []
        for j in due[:limit]:
            j["status"], j["claimed_at"] = "DIALING", now
            out.append(dict(j))
        return out

    async def claim_outbound_job(self, org_id, job_id, now):
        j = self.jobs.get(job_id)
        if not j or j["org_id"] != org_id or j["status"] != "SCHEDULED" or j["next_attempt_at"] > now:
            return None
        j["status"], j["claimed_at"] = "DIALING", now
        return dict(j)

    async def mark_outbound_dialed(self, org_id, job_id, call_sid):
        j = self.jobs.get(job_id)
        if j and j["org_id"] == org_id:
            j["attempts"] += 1
            j["last_call_sid"] = call_sid

    async def reschedule_outbound_job(self, org_id, job_id, when):
        j = self.jobs.get(job_id)
        if j and j["org_id"] == org_id and j["status"] == "DIALING":
            j["status"], j["next_attempt_at"], j["claimed_at"] = "SCHEDULED", when, None

    async def finish_outbound_attempt(self, org_id, *, call_status, now, retry_delay, call_sid=None, job_id=None):
        j = next((j for j in self.jobs.values() if j["org_id"] == org_id and
                  ((call_sid and j["last_call_sid"] == call_sid) or (job_id and j["id"] == job_id))), None)
        if j is None or j["status"] != "DIALING":
            return None
        if job_id and not call_sid:
            j["attempts"] += 1  # el intento falló antes de obtener una llamada: cuenta igualmente
        j["last_result"] = call_status
        if call_status in ("COMPLETED", "TRANSFERRED"):
            self._finish(j, "COMPLETED", now)
        elif call_status == "CANCELLED":
            self._finish(j, "CANCELLED", now)
        elif j["attempts"] >= j["max_attempts"]:
            self._finish(j, "EXHAUSTED", now)
        else:
            j["status"], j["claimed_at"] = "SCHEDULED", None
            j["next_attempt_at"] = now + retry_delay(j["attempts"])
        return dict(j)

    async def add_dnc(self, org_id, number_hash, reason, by):
        old = next((d for d in self.dnc.values() if d["org_id"] == org_id and d["number_hash"] == number_hash), None)
        if old:
            return {**old, "created": False}
        did = str(uuid.uuid4())
        self.dnc[did] = {"id": did, "org_id": org_id, "number_hash": number_hash, "reason": reason, "added_by": by,
                         "created_at": time.time()}
        for j in self.jobs.values():  # una exclusión nueva cancela lo programado para ese número
            if j["org_id"] == org_id and j["number_hash"] == number_hash and j["status"] == "SCHEDULED":
                self._finish(j, "CANCELLED", datetime.now(timezone.utc))
                j["last_result"] = "do_not_call"
        return {**self.dnc[did], "created": True}

    async def is_do_not_call(self, org_id, number_hash):
        return any(d["org_id"] == org_id and d["number_hash"] == number_hash for d in self.dnc.values())

    async def list_dnc(self, org_id):
        return [dict(d) for d in self.dnc.values() if d["org_id"] == org_id]

    async def remove_dnc(self, org_id, dnc_id):
        d = self.dnc.get(dnc_id)
        if not d or d["org_id"] != org_id:
            return False
        del self.dnc[dnc_id]
        return True

    async def resolve_phone_number(self, e164):
        n = next((n for n in self.phones.values() if n["e164"] == e164), None)
        if n is None:
            return None
        a = self.agents.get(n["agent_id"]) if n["agent_id"] else None
        return {"org_id": n["org_id"], "agent_id": n["agent_id"], "agent_status": a["status"] if a else None}

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
