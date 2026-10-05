"""Repositorio PostgreSQL para las herramientas de negocio. Todas las consultas pasan por
org_transaction (RLS) y además filtran explícitamente por org_id (defensa en profundidad)."""
import secrets
from typing import Any

from sqlalchemy import text

from app.database.engine import org_transaction


class PostgresRepository:
    async def inventory(self, org_id: str, product: str, color: str | None, size: str | None) -> dict[str, Any]:
        async with org_transaction(org_id) as c:
            row = (
                await c.execute(
                    text(
                        "SELECT units, price FROM inventory WHERE org_id = :o AND lower(product) = lower(:p) "
                        "AND (:c IS NULL OR lower(color) = lower(:c)) AND (:s IS NULL OR upper(size) = upper(:s)) "
                        "ORDER BY units DESC LIMIT 1"
                    ),
                    {"o": org_id, "p": product, "c": color, "s": size},
                )
            ).first()
        if not row:
            return {"available": False, "units": 0}
        return {"available": row.units > 0, "units": row.units, "price": float(row.price or 0)}

    async def get_reservation(self, org_id: str, reservation_id: str) -> dict[str, Any] | None:
        async with org_transaction(org_id) as c:
            row = (
                await c.execute(
                    text("SELECT id, name, date, time, party_size FROM reservations WHERE org_id = :o AND id = :i"),
                    {"o": org_id, "i": reservation_id},
                )
            ).first()
        if not row:
            return None
        return {"reservation_id": row.id, "name": row.name, "date": str(row.date), "time": str(row.time)[:5], "party_size": row.party_size}

    async def create_reservation(self, org_id: str, data: dict[str, Any]) -> dict[str, Any]:
        rid = secrets.token_hex(4).upper()
        async with org_transaction(org_id) as c:
            await c.execute(
                text(
                    "INSERT INTO reservations (id, org_id, name, date, time, party_size) "
                    "VALUES (:i, :o, :n, CAST(:d AS date), CAST(:t AS time), :ps)"
                ),
                {"i": rid, "o": org_id, "n": data["name"], "d": data["date"], "t": data["time"], "ps": data["party_size"]},
            )
        return {"reservation_id": rid, **data}

    async def create_ticket(self, org_id: str, data: dict[str, Any]) -> dict[str, Any]:
        tid = secrets.token_hex(4).upper()
        async with org_transaction(org_id) as c:
            await c.execute(
                text(
                    "INSERT INTO support_tickets (id, org_id, subject, description, priority) "
                    "VALUES (:i, :o, :s, :d, :p)"
                ),
                {"i": tid, "o": org_id, "s": data["subject"], "d": data["description"], "p": data["priority"]},
            )
        return {"ticket_id": tid, **data}

    async def search_kb(self, org_id: str, query: str) -> list[dict[str, Any]]:
        async with org_transaction(org_id) as c:
            rows = (
                await c.execute(
                    text(
                        "SELECT title, left(body, 240) AS snippet FROM knowledge_base WHERE org_id = :o "
                        "AND to_tsvector('spanish', title || ' ' || body) @@ plainto_tsquery('spanish', :q) LIMIT 5"
                    ),
                    {"o": org_id, "q": query},
                )
            ).all()
        return [{"title": r.title, "snippet": r.snippet} for r in rows]
