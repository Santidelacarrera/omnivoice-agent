"""Repositorio PostgreSQL para las herramientas de negocio. Todas las consultas pasan por
org_transaction (RLS) y además filtran explícitamente por org_id (defensa en profundidad)."""
import secrets
from datetime import date, time
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
                        "AND (CAST(:c AS text) IS NULL OR lower(color) = lower(CAST(:c AS text))) "
                        "AND (CAST(:s AS text) IS NULL OR upper(size) = upper(CAST(:s AS text))) "
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
                    text("SELECT id, name, date, time, party_size, status FROM reservations WHERE org_id = :o AND id = :i"),
                    {"o": org_id, "i": reservation_id},
                )
            ).first()
        if not row:
            return None
        return {"reservation_id": row.id, "name": row.name, "date": str(row.date), "time": str(row.time)[:5], "party_size": row.party_size,
                "status": row.status}

    async def create_reservation(self, org_id: str, data: dict[str, Any]) -> dict[str, Any]:
        rid = secrets.token_hex(4).upper()
        async with org_transaction(org_id) as c:
            await c.execute(
                text(
                    "INSERT INTO reservations (id, org_id, name, date, time, party_size) "
                    "VALUES (:i, :o, :n, :d, :t, :ps)"
                ),
                {"i": rid, "o": org_id, "n": data["name"], "d": date.fromisoformat(data["date"]), "t": time.fromisoformat(data["time"]), "ps": data["party_size"]},
            )
        return {"reservation_id": rid, **data, "status": "confirmed"}

    async def update_reservation(self, org_id: str, reservation_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        cols, params = [], {"o": org_id, "i": reservation_id}
        if "date" in changes:
            cols.append("date = :d")
            params["d"] = date.fromisoformat(changes["date"])
        if "time" in changes:
            cols.append("time = :t")
            params["t"] = time.fromisoformat(changes["time"])
        if "party_size" in changes:
            cols.append("party_size = :ps")
            params["ps"] = changes["party_size"]
        if not cols:
            return await self.get_reservation(org_id, reservation_id)
        async with org_transaction(org_id) as c:
            n = (await c.execute(text("UPDATE reservations SET " + ", ".join(cols) + ", updated_at = now() "  # noqa: S608 - columnas de una lista cerrada
                                      "WHERE org_id = :o AND id = :i AND status = 'confirmed'"), params)).rowcount
        return await self.get_reservation(org_id, reservation_id) if n else None

    async def cancel_reservation(self, org_id: str, reservation_id: str) -> dict[str, Any] | None:
        async with org_transaction(org_id) as c:
            await c.execute(text("UPDATE reservations SET status = 'cancelled', updated_at = now() WHERE org_id = :o AND id = :i"),
                            {"o": org_id, "i": reservation_id})
        return await self.get_reservation(org_id, reservation_id)

    async def find_customer(self, org_id: str, phone_hash: str) -> dict[str, Any] | None:
        async with org_transaction(org_id) as c:
            r = (await c.execute(text("SELECT id, first_name, created_at FROM customers WHERE org_id = :o AND phone_hash = :h"),
                                 {"o": org_id, "h": phone_hash})).first()
        return {"id": str(r[0]), "first_name": r[1], "created_at": r[2].timestamp()} if r else None

    async def create_customer(self, org_id: str, phone_hash: str, first_name: str) -> dict[str, Any]:
        async with org_transaction(org_id) as c:
            await c.execute(text("INSERT INTO customers (org_id, phone_hash, first_name) VALUES (:o, :h, :n) "
                                 "ON CONFLICT (org_id, phone_hash) DO NOTHING"), {"o": org_id, "h": phone_hash, "n": first_name})
        return await self.find_customer(org_id, phone_hash) or {}

    async def list_customers(self, org_id: str) -> list[dict[str, Any]]:
        async with org_transaction(org_id) as c:
            rows = (await c.execute(text("SELECT id, first_name, created_at FROM customers WHERE org_id = :o ORDER BY created_at DESC LIMIT 500"),
                                    {"o": org_id})).all()
        return [{"id": str(r[0]), "first_name": r[1], "created_at": r[2].timestamp()} for r in rows]

    async def delete_customer(self, org_id: str, customer_id: str) -> bool:
        async with org_transaction(org_id) as c:
            return bool((await c.execute(text("DELETE FROM customers WHERE id = :i AND org_id = :o"), {"i": customer_id, "o": org_id})).rowcount)

    async def get_order(self, org_id: str, order_id: str) -> dict[str, Any] | None:
        async with org_transaction(org_id) as c:
            r = (await c.execute(text("SELECT id, phone_hash, status, summary, total, eta_date FROM orders WHERE org_id = :o AND id = :i"),
                                 {"o": org_id, "i": order_id})).first()
        return ({"order_id": r[0], "phone_hash": r[1], "status": r[2], "summary": r[3], "total": float(r[4]) if r[4] is not None else None,
                 "eta_date": str(r[5]) if r[5] else None} if r else None)

    async def upsert_order(self, org_id: str, order_id: str, data: dict[str, Any]) -> dict[str, Any]:
        eta = date.fromisoformat(data["eta_date"]) if data.get("eta_date") else None
        async with org_transaction(org_id) as c:
            await c.execute(
                text("INSERT INTO orders (org_id, id, phone_hash, status, summary, total, eta_date) VALUES (:o, :i, :h, :s, :m, :t, :e) "
                     "ON CONFLICT (org_id, id) DO UPDATE SET phone_hash = EXCLUDED.phone_hash, status = EXCLUDED.status, "
                     "summary = EXCLUDED.summary, total = EXCLUDED.total, eta_date = EXCLUDED.eta_date, updated_at = now()"),
                {"o": org_id, "i": order_id, "h": data["phone_hash"], "s": data["status"], "m": data["summary"], "t": data.get("total"), "e": eta})
        return {"order_id": order_id, **data}

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

    @staticmethod
    def _or_query(query: str) -> tuple[str, dict[str, str]]:
        """Consulta «cualquiera de las palabras» (OR): una pregunta en lenguaje natural trae palabras que el documento no usa, y con AND
        (plainto_tsquery) no encontraría nada. Cada palabra pasa por `plainto_tsquery` como parámetro, así que no hay inyección ni errores
        de sintaxis; las palabras vacías (artículos, preposiciones) producen una consulta vacía que no aporta nada."""
        import re

        words = list(dict.fromkeys(w.lower() for w in re.findall(r"\w{3,}", query, re.UNICODE)))[:8]
        expr = " || ".join(f"plainto_tsquery('spanish', :w{i})" for i in range(len(words))) or "plainto_tsquery('spanish', '')"
        return expr, {f"w{i}": w for i, w in enumerate(words)}

    async def search_kb(self, org_id: str, query: str) -> list[dict[str, Any]]:
        """Documentos (fragmentos con fuente) y filas heredadas de `knowledge_base`, ordenados por relevancia."""
        expr, params = self._or_query(query)
        async with org_transaction(org_id) as c:
            rows = (await c.execute(text(  # noqa: S608 - `expr` se construye solo con parámetros enlazados
                "SELECT s.name AS title, left(k.body, 400) AS snippet, s.id AS source_id, k.ordinal AS ordinal, ts_rank(k.tsv, qq.q) AS r "
                "FROM kb_chunks k JOIN kb_sources s ON s.id = k.source_id, (SELECT " + expr + " AS q) qq "  # noqa: S608 - `expr` usa solo parámetros enlazados
                "WHERE k.org_id = :o AND k.tsv @@ qq.q ORDER BY r DESC LIMIT 5"), {"o": org_id, **params})).all()
            legacy = (await c.execute(text(  # noqa: S608
                "SELECT title, left(body, 240) AS snippet FROM knowledge_base, (SELECT " + expr + " AS q) qq WHERE org_id = :o "  # noqa: S608 - `expr` usa solo parámetros enlazados
                "AND to_tsvector('spanish', title || ' ' || body) @@ qq.q LIMIT 5"), {"o": org_id, **params})).all()
        out = [{"title": r.title, "snippet": r.snippet, "source_id": str(r.source_id), "ordinal": r.ordinal} for r in rows]
        return (out + [{"title": r.title, "snippet": r.snippet} for r in legacy])[:5]

    async def kb_create(self, org_id: str, name: str, kind: str, checksum: str, size: int, chunks: list[str], created_by: str | None) -> dict[str, Any]:
        async with org_transaction(org_id) as c:
            r = (await c.execute(text(
                "INSERT INTO kb_sources (org_id, name, kind, bytes, chars, chunk_count, checksum, created_by) "
                "VALUES (:o, :n, :k, :b, :ch, :cc, :cs, :by) RETURNING id, version, created_at, updated_at"),
                {"o": org_id, "n": name, "k": kind, "b": size, "ch": sum(len(x) for x in chunks), "cc": len(chunks), "cs": checksum, "by": created_by})).first()
            await self._kb_insert_chunks(c, org_id, str(r[0]), chunks)
        return {"id": str(r[0]), "name": name, "kind": kind, "version": r[1], "bytes": size, "chars": sum(len(x) for x in chunks),
                "chunk_count": len(chunks), "checksum": checksum, "created_by": created_by, "created_at": r[2].timestamp(), "updated_at": r[3].timestamp()}

    @staticmethod
    async def _kb_insert_chunks(c, org_id: str, source_id: str, chunks: list[str]) -> None:
        for i, body in enumerate(chunks):
            await c.execute(text("INSERT INTO kb_chunks (org_id, source_id, ordinal, body) VALUES (:o, :s, :i, :b)"),
                            {"o": org_id, "s": source_id, "i": i, "b": body})

    _KB_COLS = "id, name, kind, version, bytes, chars, chunk_count, checksum, created_by, created_at, updated_at"

    @staticmethod
    def _kb_row(r) -> dict[str, Any]:
        return {"id": str(r[0]), "name": r[1], "kind": r[2], "version": r[3], "bytes": r[4], "chars": r[5], "chunk_count": r[6],
                "checksum": r[7], "created_by": r[8], "created_at": r[9].timestamp(), "updated_at": r[10].timestamp()}

    async def kb_replace(self, org_id: str, source_id: str, name: str, kind: str, checksum: str, size: int, chunks: list[str]) -> dict[str, Any] | None:
        async with org_transaction(org_id) as c:
            n = (await c.execute(text(
                "UPDATE kb_sources SET name = :n, kind = :k, bytes = :b, chars = :ch, chunk_count = :cc, checksum = :cs, version = version + 1, "
                "updated_at = now() WHERE id = :i AND org_id = :o"),
                {"n": name, "k": kind, "b": size, "ch": sum(len(x) for x in chunks), "cc": len(chunks), "cs": checksum, "i": source_id, "o": org_id})).rowcount
            if not n:
                return None
            await c.execute(text("DELETE FROM kb_chunks WHERE source_id = :i AND org_id = :o"), {"i": source_id, "o": org_id})
            await self._kb_insert_chunks(c, org_id, source_id, chunks)
        return await self.kb_get(org_id, source_id)

    async def kb_list(self, org_id: str) -> list[dict[str, Any]]:
        async with org_transaction(org_id) as c:
            rows = (await c.execute(text("SELECT " + self._KB_COLS + " FROM kb_sources WHERE org_id = :o ORDER BY updated_at DESC"), {"o": org_id})).all()  # noqa: S608
        return [self._kb_row(r) for r in rows]

    async def kb_get(self, org_id: str, source_id: str) -> dict[str, Any] | None:
        async with org_transaction(org_id) as c:
            r = (await c.execute(text("SELECT " + self._KB_COLS + " FROM kb_sources WHERE id = :i AND org_id = :o"), {"i": source_id, "o": org_id})).first()  # noqa: S608
        return self._kb_row(r) if r else None

    async def kb_delete(self, org_id: str, source_id: str) -> bool:
        async with org_transaction(org_id) as c:
            return bool((await c.execute(text("DELETE FROM kb_sources WHERE id = :i AND org_id = :o"), {"i": source_id, "o": org_id})).rowcount)

    async def kb_totals(self, org_id: str) -> tuple[int, int]:
        async with org_transaction(org_id) as c:
            r = (await c.execute(text("SELECT count(*), COALESCE(sum(chunk_count), 0) FROM kb_sources WHERE org_id = :o"), {"o": org_id})).first()
        return int(r[0]), int(r[1])
