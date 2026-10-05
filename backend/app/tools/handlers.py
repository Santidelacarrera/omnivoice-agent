"""Handlers de negocio. Acceden a datos mediante un repositorio inyectable (PostgreSQL en prod,
en memoria en tests). Siempre filtran por org_id del principal (aislamiento multi-tenant)."""
import uuid
from typing import Any, Protocol

from pydantic import BaseModel

from app.security.auth import Principal
from app.tools.registry import (
    CheckInventoryArgs,
    CheckReservationArgs,
    CreateReservationArgs,
    CreateTicketArgs,
    SearchKbArgs,
    Tool,
    ToolRegistry,
    TransferArgs,
)


class Repository(Protocol):
    async def inventory(self, org_id: str, product: str, color: str | None, size: str | None) -> dict[str, Any]: ...
    async def get_reservation(self, org_id: str, reservation_id: str) -> dict[str, Any] | None: ...
    async def create_reservation(self, org_id: str, data: dict[str, Any]) -> dict[str, Any]: ...
    async def create_ticket(self, org_id: str, data: dict[str, Any]) -> dict[str, Any]: ...
    async def search_kb(self, org_id: str, query: str) -> list[dict[str, Any]]: ...


DEMO_ORG = "00000000-0000-0000-0000-000000000001"  # misma organización que migrations/003_*.sql


class InMemoryRepository:
    def __init__(self) -> None:
        self.stock = {(org, "chaqueta", "negro", "M"): {"units": 4, "price": 89.9} for org in ("o1", DEMO_ORG)}
        self.reservations: dict[tuple[str, str], dict[str, Any]] = {}
        self.tickets: list[dict[str, Any]] = []

    async def inventory(self, org_id, product, color, size):
        item = self.stock.get((org_id, product.lower(), (color or "").lower(), (size or "").upper()))
        return {"available": bool(item and item["units"] > 0), **(item or {"units": 0})}

    async def get_reservation(self, org_id, reservation_id):
        return self.reservations.get((org_id, reservation_id))

    async def create_reservation(self, org_id, data):
        rid = uuid.uuid4().hex[:8].upper()
        rec = {"reservation_id": rid, **data}
        self.reservations[(org_id, rid)] = rec
        return rec

    async def create_ticket(self, org_id, data):
        rec = {"ticket_id": uuid.uuid4().hex[:8].upper(), **data}
        self.tickets.append({"org_id": org_id, **rec})
        return rec

    async def search_kb(self, org_id, query):
        return [{"title": "Política de devoluciones", "snippet": "30 días con ticket."}]


def build_registry(repo: Repository) -> ToolRegistry:
    reg = ToolRegistry()

    async def check_inventory(a: CheckInventoryArgs, p: Principal):
        return await repo.inventory(p.org_id, a.product, a.color, a.size)

    async def check_reservation(a: CheckReservationArgs, p: Principal):
        res = await repo.get_reservation(p.org_id, a.reservation_id)
        return res or {"found": False}

    async def create_reservation(a: CreateReservationArgs, p: Principal):
        return await repo.create_reservation(p.org_id, a.model_dump())

    async def create_ticket(a: CreateTicketArgs, p: Principal):
        return await repo.create_ticket(p.org_id, a.model_dump())

    async def search_kb(a: SearchKbArgs, p: Principal):
        return {"results": await repo.search_kb(p.org_id, a.query)}

    async def transfer(a: TransferArgs, p: Principal):
        return {"transfer": "queued", "reason": a.reason}

    for t in [
        Tool("check_inventory", "Verifica existencias de un producto.", CheckInventoryArgs, "session:create", check_inventory),
        Tool("check_reservation", "Consulta una reserva por id.", CheckReservationArgs, "session:create", check_reservation),
        Tool("create_reservation", "Crea una reserva.", CreateReservationArgs, "session:create", create_reservation, mutating=True),
        Tool("create_support_ticket", "Abre un ticket de soporte.", CreateTicketArgs, "session:create", create_ticket, mutating=True),
        Tool("search_knowledge_base", "Busca en la base de conocimiento.", SearchKbArgs, "session:create", search_kb),
        Tool("transfer_to_human", "Deriva a un operador humano.", TransferArgs, "session:create", transfer, mutating=True),
    ]:
        reg.register(t)
    return reg
