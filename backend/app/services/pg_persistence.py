"""Persistencia PostgreSQL (RLS por organización). Módulo aparte para que el modo en memoria
no requiera SQLAlchemy ni asyncpg."""
import json
import uuid

import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database.engine import get_engine, org_transaction
from app.observability.metrics import ERRORS
from app.services.persistence import _VERSIONED, COST_PER_AUDIO_MINUTE_USD, OutboundRejected, PhoneNumberTaken
from app.telephony.call_state import can_transition, is_terminal

log = structlog.get_logger()


def _safe(fn):
    async def wrapper(*a, **k):
        try:
            return await fn(*a, **k)
        except Exception:  # noqa: BLE001 - persistir nunca debe tumbar la llamada
            ERRORS.labels("persistence").inc()
            log.exception("persistence_failed", op=fn.__name__)
            return None

    wrapper.__name__ = fn.__name__
    return wrapper


class PostgresPersistence:
    @_safe
    async def audit(self, org_id, actor, action, detail):
        if not org_id:
            return
        async with org_transaction(org_id) as c:
            await c.execute(
                text("INSERT INTO audit_logs (org_id, actor, action, detail) VALUES (:o, :a, :ac, CAST(:d AS jsonb))"),
                {"o": org_id, "a": actor, "ac": action, "d": json.dumps(detail, default=str)},
            )

    async def start_conversation(self, org_id, session_id, user_id):
        sid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        try:
            async with org_transaction(org_id) as c:
                await c.execute(
                    text("INSERT INTO sessions (id, org_id, state) VALUES (:s, :o, 'LISTENING')"), {"s": sid, "o": org_id}
                )
                await c.execute(
                    text("INSERT INTO conversations (id, session_id, org_id) VALUES (:c, :s, :o)"),
                    {"c": cid, "s": sid, "o": org_id},
                )
        except Exception:  # noqa: BLE001
            ERRORS.labels("persistence").inc()
            log.exception("persistence_failed", op="start_conversation")
        return cid

    @_safe
    async def end_conversation(self, org_id, conversation_id, summary):
        async with org_transaction(org_id) as c:
            await c.execute(
                text(
                    "UPDATE conversations SET final_state = :fs, first_audio_ms = :fa, interruptions = :i, "
                    "lost_packets = :lp WHERE id = :c AND org_id = :o"
                ),
                {"fs": summary.get("final_state"), "fa": summary.get("first_audio_ms"),
                 "i": summary.get("interruptions", 0), "lp": summary.get("lost_packets", 0),
                 "c": conversation_id, "o": org_id},
            )
            secs = float(summary.get("audio_seconds", 0))
            await c.execute(
                text(
                    "INSERT INTO usage_records (org_id, audio_seconds, estimated_cost_usd) VALUES (:o, :s, :cost)"
                ),
                {"o": org_id, "s": secs, "cost": round(secs / 60 * COST_PER_AUDIO_MINUTE_USD, 4)},
            )

    @_safe
    async def add_transcript(self, org_id, conversation_id, speaker, content):
        async with org_transaction(org_id) as c:
            await c.execute(
                text("INSERT INTO transcripts (conversation_id, org_id, speaker, text) VALUES (:c, :o, :s, :t)"),
                {"c": conversation_id, "o": org_id, "s": speaker, "t": content},
            )

    @_safe
    async def add_event(self, org_id, conversation_id, type_, payload, correlation_id):
        async with org_transaction(org_id) as c:
            await c.execute(
                text(
                    "INSERT INTO conversation_events (conversation_id, org_id, type, payload, correlation_id) "
                    "VALUES (:c, :o, :t, CAST(:p AS jsonb), :k)"
                ),
                {"c": conversation_id, "o": org_id, "t": type_, "p": json.dumps(payload, default=str), "k": correlation_id},
            )

    @_safe
    async def record_tool(self, org_id, conversation_id, rec):
        async with org_transaction(org_id) as c:
            await c.execute(
                text(
                    "INSERT INTO tool_executions (conversation_id, org_id, tool_name, call_id, args, result, ok, duration_ms) "
                    "VALUES (:c, :o, :t, :k, CAST(:a AS jsonb), CAST(:r AS jsonb), :ok, :d) "
                    "ON CONFLICT (org_id, call_id, tool_name) DO NOTHING"
                ),
                {"c": conversation_id, "o": org_id, "t": rec["tool"], "k": rec["call_id"],
                 "a": json.dumps(rec.get("args", {}), default=str), "r": json.dumps(rec.get("result", {}), default=str),
                 "ok": rec.get("ok"), "d": rec.get("duration_ms")},
            )

    async def list_conversations(self, org_id, limit=50):
        async with org_transaction(org_id) as c:
            rows = (await c.execute(
                text("SELECT id, created_at, final_state, first_audio_ms, interruptions, lost_packets "
                     "FROM conversations WHERE org_id = :o ORDER BY created_at DESC LIMIT :l"),
                {"o": org_id, "l": limit})).mappings().all()
        return [{**r, "id": str(r["id"]), "created_at": r["created_at"].timestamp()} for r in rows]

    async def get_conversation(self, org_id, conversation_id):
        async with org_transaction(org_id) as c:
            conv = (await c.execute(text("SELECT * FROM conversations WHERE id = :c AND org_id = :o"),
                                    {"c": conversation_id, "o": org_id})).mappings().first()
            if not conv:
                return None
            tr = (await c.execute(text("SELECT speaker, text, ts FROM transcripts WHERE conversation_id = :c ORDER BY ts"),
                                  {"c": conversation_id})).mappings().all()
            tools = (await c.execute(text("SELECT tool_name, ok, duration_ms, ts FROM tool_executions WHERE conversation_id = :c ORDER BY ts"),
                                     {"c": conversation_id})).mappings().all()
        return {"id": str(conv["id"]), "final_state": conv["final_state"], "first_audio_ms": conv["first_audio_ms"],
                "interruptions": conv["interruptions"], "lost_packets": conv["lost_packets"],
                "transcript": [{**t, "ts": t["ts"].timestamp()} for t in tr],
                "tools": [{**t, "ts": t["ts"].timestamp()} for t in tools]}

    async def list_audit(self, org_id, limit=200):
        async with org_transaction(org_id) as c:
            rows = (await c.execute(
                text("SELECT actor, action, detail, ts FROM audit_logs WHERE org_id = :o ORDER BY ts DESC LIMIT :l"),
                {"o": org_id, "l": limit})).mappings().all()
        return [{**r, "ts": r["ts"].timestamp()} for r in rows]

    async def list_agents(self, org_id):
        async with org_transaction(org_id) as c:
            rows = (await c.execute(
                text("SELECT a.id, a.name, a.instructions, a.voice, a.language, a.status, a.version, "
                     "COALESCE(array_agg(t.tool_name) FILTER (WHERE t.enabled), '{}') AS tools "
                     "FROM agents a LEFT JOIN agent_tools t ON t.agent_id = a.id "
                     "WHERE a.org_id = :o GROUP BY a.id ORDER BY a.created_at"), {"o": org_id})).mappings().all()
        return [{**r, "id": str(r["id"]), "tools": list(r["tools"])} for r in rows]

    async def get_agent(self, org_id, agent_id):
        return next((a for a in await self.list_agents(org_id) if a["id"] == agent_id), None)

    async def create_agent(self, org_id, name, instructions, tools, voice, language):
        aid = str(uuid.uuid4())
        agent = {"id": aid, "name": name, "instructions": instructions, "tools": list(tools), "voice": voice,
                 "language": language, "status": "draft", "version": 1}
        async with org_transaction(org_id) as c:
            await c.execute(
                text("INSERT INTO agents (id, org_id, name, instructions, voice, language) "
                     "VALUES (:i, :o, :n, :ins, :v, :l)"),
                {"i": aid, "o": org_id, "n": name, "ins": instructions, "v": voice, "l": language})
            for tool in tools:
                await c.execute(text("INSERT INTO agent_tools (agent_id, tool_name) VALUES (:a, :t)"), {"a": aid, "t": tool})
            await self._snapshot(c, org_id, agent)
        return agent

    @staticmethod
    async def _snapshot(c, org_id, agent):
        snap = {"version": agent["version"], **{k: agent[k] for k in _VERSIONED}}
        await c.execute(text("INSERT INTO agent_versions (agent_id, org_id, version, snapshot) "
                             "VALUES (:a, :o, :v, CAST(:s AS jsonb))"),
                        {"a": agent["id"], "o": org_id, "v": agent["version"], "s": json.dumps(snap)})

    @staticmethod
    async def _current(c, org_id, agent_id):
        r = (await c.execute(
            text("SELECT a.id, a.name, a.instructions, a.voice, a.language, a.status, a.version, "
                 "COALESCE(array_agg(t.tool_name) FILTER (WHERE t.enabled), '{}') AS tools "
                 "FROM agents a LEFT JOIN agent_tools t ON t.agent_id = a.id "
                 "WHERE a.id = :a AND a.org_id = :o GROUP BY a.id"), {"a": agent_id, "o": org_id})).mappings().first()
        return {**r, "id": str(r["id"]), "tools": sorted(r["tools"])} if r else None

    async def update_agent(self, org_id, agent_id, changes):
        async with org_transaction(org_id) as c:
            locked = (await c.execute(text("SELECT status FROM agents WHERE id = :a AND org_id = :o FOR UPDATE"),
                                      {"a": agent_id, "o": org_id})).first()
            if not locked or locked[0] == "archived":
                return None
            cur = dict(await self._current(c, org_id, agent_id))
            cur.update(changes)
            cur["version"] += 1
            await c.execute(
                text("UPDATE agents SET name=:n, instructions=:ins, voice=:v, language=:l, version=:ver "
                     "WHERE id=:a AND org_id=:o"),
                {"n": cur["name"], "ins": cur["instructions"], "v": cur["voice"], "l": cur["language"],
                 "ver": cur["version"], "a": agent_id, "o": org_id})
            await c.execute(text("DELETE FROM agent_tools WHERE agent_id = :a"), {"a": agent_id})
            for tool in cur["tools"]:
                await c.execute(text("INSERT INTO agent_tools (agent_id, tool_name) VALUES (:a, :t)"), {"a": agent_id, "t": tool})
            await self._snapshot(c, org_id, cur)
        return cur

    async def list_agent_versions(self, org_id, agent_id):
        async with org_transaction(org_id) as c:
            if not (await c.execute(text("SELECT 1 FROM agents WHERE id = :a AND org_id = :o"),
                                    {"a": agent_id, "o": org_id})).first():
                return None
            rows = (await c.execute(text("SELECT snapshot, created_at FROM agent_versions WHERE agent_id = :a "
                                         "AND org_id = :o ORDER BY version"), {"a": agent_id, "o": org_id})).all()
        return [{**(r[0] if isinstance(r[0], dict) else json.loads(r[0])), "created_at": r[1].timestamp()} for r in rows]

    async def rollback_agent(self, org_id, agent_id, version):
        versions = await self.list_agent_versions(org_id, agent_id)
        old = next((v for v in versions or [] if v["version"] == version), None)
        if old is None:
            return None
        return await self.update_agent(org_id, agent_id, {k: old[k] for k in _VERSIONED})

    async def set_agent_status(self, org_id, agent_id, status):
        async with org_transaction(org_id) as c:
            n = (await c.execute(text("UPDATE agents SET status = :s WHERE id = :a AND org_id = :o"),
                                 {"s": status, "a": agent_id, "o": org_id})).rowcount
            return await self._current(c, org_id, agent_id) if n else None

    async def list_phone_numbers(self, org_id):
        async with org_transaction(org_id) as c:
            rows = (await c.execute(text("SELECT id, e164, agent_id FROM phone_numbers WHERE org_id = :o ORDER BY created_at"),
                                    {"o": org_id})).all()
        return [{"id": str(r[0]), "e164": r[1], "agent_id": str(r[2]) if r[2] else None} for r in rows]

    async def add_phone_number(self, org_id, e164, agent_id):
        pid = str(uuid.uuid4())
        try:
            async with org_transaction(org_id) as c:
                await c.execute(text("INSERT INTO phone_numbers (id, org_id, e164, agent_id) VALUES (:i, :o, :e, :a)"),
                                {"i": pid, "o": org_id, "e": e164, "a": agent_id})
        except IntegrityError as exc:
            raise PhoneNumberTaken(e164) from exc
        return {"id": pid, "e164": e164, "agent_id": agent_id}

    @_safe
    async def upsert_call(self, org_id, provider, sid, direction, agent_id, our_number, peer_hash):
        async with org_transaction(org_id) as c:
            row = (await c.execute(
                text("INSERT INTO calls (org_id, provider, provider_call_sid, direction, agent_id, our_number, peer_hash) "
                     "VALUES (:o, :p, :s, :d, :a, :n, :h) ON CONFLICT (provider, provider_call_sid) DO NOTHING "
                     "RETURNING id, status"),
                {"o": org_id, "p": provider, "s": sid, "d": direction, "a": agent_id, "n": our_number, "h": peer_hash})).first()
            if row:
                await c.execute(text("INSERT INTO call_events (call_id, org_id, status, event_key, applied) "
                                     "VALUES (:c, :o, 'CREATED', 'created', true)"), {"c": row[0], "o": org_id})
                return {"id": str(row[0]), "status": row[1], "created": True}
            row = (await c.execute(text("SELECT id, status FROM calls WHERE provider = :p AND provider_call_sid = :s AND org_id = :o"),
                                   {"p": provider, "s": sid, "o": org_id})).first()
        return {"id": str(row[0]), "status": row[1], "created": False} if row else None

    @_safe
    async def apply_call_event(self, org_id, provider, sid, status, event_key, detail):
        async with org_transaction(org_id) as c:
            call = (await c.execute(
                text("SELECT id, status FROM calls WHERE provider = :p AND provider_call_sid = :s AND org_id = :o FOR UPDATE"),
                {"p": provider, "s": sid, "o": org_id})).first()
            if not call:
                return None
            cid, current = call
            applied = can_transition(current, status)
            ins = (await c.execute(
                text("INSERT INTO call_events (call_id, org_id, status, event_key, applied, detail) "
                     "VALUES (:c, :o, :st, :k, :ap, CAST(:d AS jsonb)) ON CONFLICT (call_id, event_key) DO NOTHING RETURNING id"),
                {"c": cid, "o": org_id, "st": status, "k": event_key, "ap": applied, "d": json.dumps(detail)})).first()
            if ins is None:
                return {"call_id": str(cid), "status": current, "applied": False, "duplicate": True}
            if applied:
                await c.execute(
                    text("UPDATE calls SET status = :st, "
                         "answered_at = CASE WHEN :st IN ('ANSWERED','CONNECTED') THEN COALESCE(answered_at, now()) ELSE answered_at END, "
                         "ended_at = CASE WHEN :term THEN now() ELSE ended_at END WHERE id = :c"),
                    {"st": status, "term": is_terminal(status), "c": cid})
        return {"call_id": str(cid), "status": status if applied else current, "applied": applied, "duplicate": False}

    async def resolve_call_org(self, provider, sid):
        try:
            async with get_engine().connect() as c:
                r = (await c.execute(text("SELECT resolve_call_org(:p, :s)"), {"p": provider, "s": sid})).scalar()
            return str(r) if r else None
        except Exception:  # noqa: BLE001 - sin seguimiento la llamada sigue funcionando
            ERRORS.labels("persistence").inc()
            log.exception("persistence_failed", op="resolve_call_org")
            return None

    @staticmethod
    def _call_row(r):
        return {"id": str(r["id"]), "provider": r["provider"], "sid": r["provider_call_sid"], "direction": r["direction"],
                "agent_id": str(r["agent_id"]) if r["agent_id"] else None, "our_number": r["our_number"],
                "peer_hash": r["peer_hash"], "status": r["status"], "created_at": r["created_at"].timestamp(),
                "answered_at": r["answered_at"].timestamp() if r["answered_at"] else None,
                "ended_at": r["ended_at"].timestamp() if r["ended_at"] else None}

    async def list_calls(self, org_id, limit=50):
        async with org_transaction(org_id) as c:
            rows = (await c.execute(text("SELECT * FROM calls WHERE org_id = :o ORDER BY created_at DESC LIMIT :l"),
                                    {"o": org_id, "l": limit})).mappings().all()
        return [self._call_row(r) for r in rows]

    async def get_call(self, org_id, call_id):
        async with org_transaction(org_id) as c:
            r = (await c.execute(text("SELECT * FROM calls WHERE id = :i AND org_id = :o"), {"i": call_id, "o": org_id})).mappings().first()
            if not r:
                return None
            ev = (await c.execute(text("SELECT status, event_key, applied, detail, ts FROM call_events WHERE call_id = :i ORDER BY id"),
                                  {"i": call_id})).mappings().all()
        return {**self._call_row(r), "events": [{**e, "ts": e["ts"].timestamp()} for e in ev]}

    # ---- llamadas salientes ----
    _JOB_COLS = ("id, org_id, number_hash, to_number, from_number, timezone, consent_basis, consent_ref, idempotency_key, status, "
                 "attempts, max_attempts, next_attempt_at, claimed_at, last_call_sid, last_result, created_by, created_at, finished_at")

    @staticmethod
    def _job(r):
        def ts(v):
            return v.timestamp() if v else None
        return {"id": str(r["id"]), "org_id": str(r["org_id"]), "number_hash": r["number_hash"], "to_number": r["to_number"],
                "from_number": r["from_number"], "timezone": r["timezone"], "consent_basis": r["consent_basis"],
                "consent_ref": r["consent_ref"], "idempotency_key": r["idempotency_key"], "status": r["status"],
                "attempts": r["attempts"], "max_attempts": r["max_attempts"], "next_attempt_at": r["next_attempt_at"],
                "claimed_at": r["claimed_at"], "last_call_sid": r["last_call_sid"], "last_result": r["last_result"],
                "created_by": r["created_by"], "created_at": ts(r["created_at"]), "finished_at": ts(r["finished_at"])}

    async def create_outbound_job(self, org_id, **f):
        try:
            async with org_transaction(org_id) as c:
                if (await c.execute(text("SELECT 1 FROM do_not_call WHERE org_id = :o AND number_hash = :h"),
                                    {"o": org_id, "h": f["number_hash"]})).first():
                    raise OutboundRejected("do_not_call")
                key = f.get("idempotency_key")
                if key:
                    old = (await c.execute(text(f"SELECT {self._JOB_COLS} FROM outbound_jobs WHERE org_id = :o AND idempotency_key = :k"),  # noqa: S608 - _JOB_COLS es una constante de la clase, sin entrada del usuario
                                           {"o": org_id, "k": key})).mappings().first()
                    if old:
                        return {"job": self._job(old), "created": False}
                row = (await c.execute(
                    text("INSERT INTO outbound_jobs (org_id, number_hash, to_number, from_number, timezone, consent_basis, consent_ref, "  # noqa: S608 - _JOB_COLS es una constante de la clase, sin entrada del usuario
                         "idempotency_key, max_attempts, next_attempt_at, created_by) VALUES (:o, :h, :t, :f, :tz, :cb, :cr, :k, :m, :n, :by) "
                         f"RETURNING {self._JOB_COLS}"),
                    {"o": org_id, "h": f["number_hash"], "t": f["to_number"], "f": f["from_number"], "tz": f["timezone"],
                     "cb": f["consent_basis"], "cr": f.get("consent_ref"), "k": key, "m": f["max_attempts"],
                     "n": f["next_attempt_at"], "by": f.get("created_by")})).mappings().first()
                return {"job": self._job(row), "created": True}
        except IntegrityError:
            # Carrera: otro trabajo activo con el mismo número, o la misma clave de idempotencia insertada a la vez.
            if f.get("idempotency_key"):
                async with org_transaction(org_id) as c:
                    old = (await c.execute(text(f"SELECT {self._JOB_COLS} FROM outbound_jobs WHERE org_id = :o AND idempotency_key = :k"),  # noqa: S608 - _JOB_COLS es una constante de la clase, sin entrada del usuario
                                           {"o": org_id, "k": f["idempotency_key"]})).mappings().first()
                if old:
                    return {"job": self._job(old), "created": False}
            raise OutboundRejected("duplicate_active") from None

    async def get_outbound_job(self, org_id, job_id):
        async with org_transaction(org_id) as c:
            r = (await c.execute(text(f"SELECT {self._JOB_COLS} FROM outbound_jobs WHERE id = :i AND org_id = :o"),  # noqa: S608 - _JOB_COLS es una constante de la clase, sin entrada del usuario
                                 {"i": job_id, "o": org_id})).mappings().first()
        return self._job(r) if r else None

    async def list_outbound_jobs(self, org_id, limit=50):
        async with org_transaction(org_id) as c:
            rows = (await c.execute(text(f"SELECT {self._JOB_COLS} FROM outbound_jobs WHERE org_id = :o ORDER BY created_at DESC LIMIT :l"),  # noqa: S608 - _JOB_COLS es una constante de la clase, sin entrada del usuario
                                    {"o": org_id, "l": limit})).mappings().all()
        return [self._job(r) for r in rows]

    async def cancel_outbound_job(self, org_id, job_id):
        async with org_transaction(org_id) as c:
            await c.execute(text("UPDATE outbound_jobs SET status = 'CANCELLED', to_number = NULL, claimed_at = NULL, finished_at = now(), "
                                 "last_result = 'cancelled_by_user' WHERE id = :i AND org_id = :o AND status = 'SCHEDULED'"),
                            {"i": job_id, "o": org_id})
        return await self.get_outbound_job(org_id, job_id)

    async def claim_due_outbound_jobs(self, now, limit, stale_s):
        async with get_engine().begin() as c:
            rows = (await c.execute(text("SELECT * FROM claim_due_outbound_jobs(:n, :l, :s)"),
                                    {"n": now, "l": limit, "s": stale_s})).mappings().all()
        return [{"id": str(r["id"]), "org_id": str(r["org_id"]), "number_hash": r["number_hash"], "to_number": r["to_number"],
                 "from_number": r["from_number"], "timezone": r["timezone"], "attempts": r["attempts"],
                 "max_attempts": r["max_attempts"]} for r in rows]

    async def claim_outbound_job(self, org_id, job_id, now):
        """Toma solo este trabajo (el de la petición en curso), sin tocar los de otras organizaciones."""
        async with org_transaction(org_id) as c:
            r = (await c.execute(
                text("UPDATE outbound_jobs SET status = 'DIALING', claimed_at = :n WHERE id = :i AND org_id = :o AND status = 'SCHEDULED' "
                     "AND next_attempt_at <= :n RETURNING id, org_id, to_number, from_number, timezone, attempts, max_attempts, number_hash"),
                {"n": now, "i": job_id, "o": org_id})).mappings().first()
        return ({"id": str(r["id"]), "org_id": str(r["org_id"]), "to_number": r["to_number"], "from_number": r["from_number"],
                 "timezone": r["timezone"], "attempts": r["attempts"], "max_attempts": r["max_attempts"],
                 "number_hash": r["number_hash"]} if r else None)

    async def mark_outbound_dialed(self, org_id, job_id, call_sid):
        async with org_transaction(org_id) as c:
            await c.execute(text("UPDATE outbound_jobs SET attempts = attempts + 1, last_call_sid = :s WHERE id = :i AND org_id = :o"),
                            {"s": call_sid, "i": job_id, "o": org_id})

    async def reschedule_outbound_job(self, org_id, job_id, when):
        async with org_transaction(org_id) as c:
            await c.execute(text("UPDATE outbound_jobs SET status = 'SCHEDULED', next_attempt_at = :w, claimed_at = NULL "
                                 "WHERE id = :i AND org_id = :o AND status = 'DIALING'"), {"w": when, "i": job_id, "o": org_id})

    async def finish_outbound_attempt(self, org_id, *, call_status, now, retry_delay, call_sid=None, job_id=None):
        async with org_transaction(org_id) as c:
            r = (await c.execute(
                text(f"SELECT {self._JOB_COLS} FROM outbound_jobs WHERE org_id = :o AND status = 'DIALING' AND "  # noqa: S608 - _JOB_COLS es una constante de la clase, sin entrada del usuario
                     "((CAST(:s AS TEXT) IS NOT NULL AND last_call_sid = :s) OR (CAST(:j AS UUID) IS NOT NULL AND id = :j)) FOR UPDATE"),
                {"o": org_id, "s": call_sid, "j": job_id})).mappings().first()
            if r is None:
                return None
            attempts = r["attempts"] + (1 if job_id and not call_sid else 0)
            if call_status in ("COMPLETED", "TRANSFERRED"):
                final = "COMPLETED"
            elif call_status == "CANCELLED":
                final = "CANCELLED"
            elif attempts >= r["max_attempts"]:
                final = "EXHAUSTED"
            else:
                final = None
            if final:
                await c.execute(text("UPDATE outbound_jobs SET status = :st, attempts = :a, last_result = :lr, to_number = NULL, "
                                     "claimed_at = NULL, finished_at = :n WHERE id = :i"),
                                {"st": final, "a": attempts, "lr": call_status, "n": now, "i": r["id"]})
            else:
                await c.execute(text("UPDATE outbound_jobs SET status = 'SCHEDULED', attempts = :a, last_result = :lr, claimed_at = NULL, "
                                     "next_attempt_at = :w WHERE id = :i"),
                                {"a": attempts, "lr": call_status, "w": now + retry_delay(attempts), "i": r["id"]})
        return await self.get_outbound_job(org_id, str(r["id"]))

    async def add_dnc(self, org_id, number_hash, reason, by):
        async with org_transaction(org_id) as c:
            row = (await c.execute(text("INSERT INTO do_not_call (org_id, number_hash, reason, added_by) VALUES (:o, :h, :r, :b) "
                                        "ON CONFLICT (org_id, number_hash) DO NOTHING RETURNING id, created_at"),
                                   {"o": org_id, "h": number_hash, "r": reason, "b": by})).first()
            created = row is not None
            if row is None:
                row = (await c.execute(text("SELECT id, created_at FROM do_not_call WHERE org_id = :o AND number_hash = :h"),
                                       {"o": org_id, "h": number_hash})).first()
            # Una exclusión nueva cancela lo que estuviera programado para ese número.
            await c.execute(text("UPDATE outbound_jobs SET status = 'CANCELLED', to_number = NULL, claimed_at = NULL, finished_at = now(), "
                                 "last_result = 'do_not_call' WHERE org_id = :o AND number_hash = :h AND status = 'SCHEDULED'"),
                            {"o": org_id, "h": number_hash})
        return {"id": str(row[0]), "number_hash": number_hash, "reason": reason, "created_at": row[1].timestamp(), "created": created}

    async def is_do_not_call(self, org_id, number_hash):
        async with org_transaction(org_id) as c:
            return (await c.execute(text("SELECT 1 FROM do_not_call WHERE org_id = :o AND number_hash = :h"),
                                    {"o": org_id, "h": number_hash})).first() is not None

    async def list_dnc(self, org_id):
        async with org_transaction(org_id) as c:
            rows = (await c.execute(text("SELECT id, number_hash, reason, created_at FROM do_not_call WHERE org_id = :o ORDER BY created_at DESC"),
                                    {"o": org_id})).all()
        return [{"id": str(r[0]), "number_hash": r[1], "reason": r[2], "created_at": r[3].timestamp()} for r in rows]

    async def remove_dnc(self, org_id, dnc_id):
        async with org_transaction(org_id) as c:
            return bool((await c.execute(text("DELETE FROM do_not_call WHERE id = :i AND org_id = :o"), {"i": dnc_id, "o": org_id})).rowcount)

    async def resolve_phone_number(self, e164):
        """Búsqueda global por número vía la función SECURITY DEFINER de la migración 006 (no hay org todavía)."""
        async with get_engine().connect() as c:
            r = (await c.execute(text("SELECT org_id, agent_id, agent_status FROM resolve_phone_number(:e)"), {"e": e164})).first()
        return {"org_id": str(r[0]), "agent_id": str(r[1]) if r[1] else None, "agent_status": r[2]} if r else None

    async def delete_phone_number(self, org_id, phone_id):
        async with org_transaction(org_id) as c:
            return bool((await c.execute(text("DELETE FROM phone_numbers WHERE id = :i AND org_id = :o"),
                                         {"i": phone_id, "o": org_id})).rowcount)

    async def usage_summary(self, org_id):
        async with org_transaction(org_id) as c:
            r = (await c.execute(
                text("SELECT COALESCE(sum(audio_seconds),0) s, COALESCE(sum(estimated_cost_usd),0) c "
                     "FROM usage_records WHERE org_id = :o"), {"o": org_id})).first()
        return {"audio_seconds": float(r.s), "estimated_cost_usd": float(r.c)}

    async def org_policy(self, org_id):
        try:
            async with org_transaction(org_id) as c:
                r = (await c.execute(text("SELECT recording_enabled FROM organizations WHERE id = :o"), {"o": org_id})).first()
            return {"recording_enabled": bool(r[0]) if r else False}
        except Exception:  # noqa: BLE001 - ante la duda, sin grabación
            ERRORS.labels("persistence").inc()
            log.exception("persistence_failed", op="org_policy")
            return {"recording_enabled": False}

    @_safe
    async def set_conversation_options(self, org_id, conversation_id, *, recording_consent, voice, language):
        async with org_transaction(org_id) as c:
            await c.execute(
                text("UPDATE conversations SET recording_consent = :rc, voice = :v, language = :l, "
                     "consent_at = CASE WHEN :rc THEN now() ELSE NULL END WHERE id = :c AND org_id = :o"),
                {"rc": recording_consent, "v": voice, "l": language, "c": conversation_id, "o": org_id})

    @_safe
    async def add_recording(self, org_id, conversation_id, key, size, duration_s):
        async with org_transaction(org_id) as c:
            await c.execute(
                text("INSERT INTO recordings (org_id, conversation_id, storage_key, bytes, duration_s) "
                     "VALUES (:o, :c, :k, :b, :d)"),
                {"o": org_id, "c": conversation_id, "k": key, "b": size, "d": round(duration_s, 2)})

    async def get_recording(self, org_id, conversation_id):
        async with org_transaction(org_id) as c:
            r = (await c.execute(
                text("SELECT storage_key, bytes, duration_s, created_at FROM recordings "
                     "WHERE conversation_id = :c AND org_id = :o ORDER BY created_at DESC LIMIT 1"),
                {"c": conversation_id, "o": org_id})).mappings().first()
        return {**r, "duration_s": float(r["duration_s"]), "created_at": r["created_at"].timestamp()} if r else None

    async def delete_conversation(self, org_id, conversation_id):
        """Derecho de supresión: la sesión arrastra (CASCADE) conversación, transcripciones, eventos y grabaciones."""
        async with org_transaction(org_id) as c:
            row = (await c.execute(
                text("SELECT c.session_id, COALESCE(array_agg(r.storage_key) FILTER (WHERE r.id IS NOT NULL), '{}') "
                     "FROM conversations c LEFT JOIN recordings r ON r.conversation_id = c.id "
                     "WHERE c.id = :c AND c.org_id = :o GROUP BY c.session_id"),
                {"c": conversation_id, "o": org_id})).first()
            if not row:
                return None
            await c.execute(text("DELETE FROM sessions WHERE id = :s AND org_id = :o"), {"s": row[0], "o": org_id})
        return list(row[1])
