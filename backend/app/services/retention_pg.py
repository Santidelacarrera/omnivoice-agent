"""Retención sobre PostgreSQL. Aparte para que el modo en memoria no requiera SQLAlchemy."""
from sqlalchemy import text

from app.database.engine import get_engine, org_transaction


class PostgresRetentionBackend:
    async def list_orgs(self):
        # `organizations` no tiene RLS; omni_app solo puede leer (id, name, retention_days).
        async with get_engine().connect() as c:
            rows = (await c.execute(text("SELECT id, retention_days FROM organizations"))).all()
        return [(str(r[0]), int(r[1])) for r in rows]

    async def expired_conversations(self, org_id, cutoff_ts, limit):
        async with org_transaction(org_id) as c:
            rows = (await c.execute(
                text("SELECT c.id, COALESCE(array_agg(r.storage_key) FILTER (WHERE r.id IS NOT NULL), '{}') AS keys "
                     "FROM conversations c LEFT JOIN recordings r ON r.conversation_id = c.id "
                     "WHERE c.org_id = :o AND c.created_at < to_timestamp(:cutoff) "
                     "GROUP BY c.id ORDER BY min(c.created_at) LIMIT :l"),
                {"o": org_id, "cutoff": cutoff_ts, "l": limit})).all()
        return [(str(r[0]), list(r[1])) for r in rows]

    async def delete_conversations(self, org_id, conversation_ids):
        # Borrar la sesión arrastra (ON DELETE CASCADE) conversación, transcripciones, eventos y grabaciones;
        # tool_executions queda con conversation_id NULL y sin contenido personal (solo claves de argumentos).
        async with org_transaction(org_id) as c:
            res = await c.execute(
                text("DELETE FROM sessions WHERE org_id = :o AND id IN "
                     "(SELECT session_id FROM conversations WHERE id = ANY(CAST(:ids AS uuid[])))"),
                {"o": org_id, "ids": conversation_ids})
            return res.rowcount or 0
