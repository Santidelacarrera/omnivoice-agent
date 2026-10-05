"""Persistencia PostgreSQL (RLS por organización). Módulo aparte para que el modo en memoria
no requiera SQLAlchemy ni asyncpg."""
import json
import uuid

import structlog
from sqlalchemy import text

from app.database.engine import org_transaction
from app.observability.metrics import ERRORS
from app.services.persistence import COST_PER_AUDIO_MINUTE_USD

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
                text("SELECT a.id, a.name, a.instructions, a.voice, a.language, "
                     "COALESCE(array_agg(t.tool_name) FILTER (WHERE t.enabled), '{}') AS tools "
                     "FROM agents a LEFT JOIN agent_tools t ON t.agent_id = a.id "
                     "WHERE a.org_id = :o GROUP BY a.id ORDER BY a.created_at"), {"o": org_id})).mappings().all()
        return [{**r, "id": str(r["id"]), "tools": list(r["tools"])} for r in rows]

    async def get_agent(self, org_id, agent_id):
        return next((a for a in await self.list_agents(org_id) if a["id"] == agent_id), None)

    async def create_agent(self, org_id, name, instructions, tools, voice, language):
        aid = str(uuid.uuid4())
        async with org_transaction(org_id) as c:
            await c.execute(
                text("INSERT INTO agents (id, org_id, name, instructions, voice, language) "
                     "VALUES (:i, :o, :n, :ins, :v, :l)"),
                {"i": aid, "o": org_id, "n": name, "ins": instructions, "v": voice, "l": language})
            for tool in tools:
                await c.execute(text("INSERT INTO agent_tools (agent_id, tool_name) VALUES (:a, :t)"), {"a": aid, "t": tool})
        return {"id": aid, "name": name, "instructions": instructions, "tools": tools, "voice": voice, "language": language}

    async def usage_summary(self, org_id):
        async with org_transaction(org_id) as c:
            r = (await c.execute(
                text("SELECT COALESCE(sum(audio_seconds),0) s, COALESCE(sum(estimated_cost_usd),0) c "
                     "FROM usage_records WHERE org_id = :o"), {"o": org_id})).first()
        return {"audio_seconds": float(r.s), "estimated_cost_usd": float(r.c)}
