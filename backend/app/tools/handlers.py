"""Handlers de negocio. Acceden a datos mediante un repositorio inyectable (PostgreSQL en prod,
en memoria en tests). Siempre filtran por org_id del principal (aislamiento multi-tenant)."""
import uuid
from typing import Any, Protocol

from app.security.auth import Principal
from app.tools.registry import (
    CancelReservationArgs,
    CheckInventoryArgs,
    CheckReservationArgs,
    CreateReservationArgs,
    CreateTicketArgs,
    EndCallArgs,
    ModifyReservationArgs,
    SearchKbArgs,
    Tool,
    ToolRegistry,
    TransferArgs,
)


class Repository(Protocol):
    async def inventory(self, org_id: str, product: str, color: str | None, size: str | None) -> dict[str, Any]: ...
    async def get_reservation(self, org_id: str, reservation_id: str) -> dict[str, Any] | None: ...
    async def create_reservation(self, org_id: str, data: dict[str, Any]) -> dict[str, Any]: ...
    async def update_reservation(self, org_id: str, reservation_id: str, changes: dict[str, Any]) -> dict[str, Any] | None: ...
    async def cancel_reservation(self, org_id: str, reservation_id: str) -> dict[str, Any] | None: ...
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
        rec = {"reservation_id": rid, **data, "status": "confirmed"}
        self.reservations[(org_id, rid)] = rec
        return rec

    async def update_reservation(self, org_id, reservation_id, changes):
        rec = self.reservations.get((org_id, reservation_id))
        if rec is None:
            return None
        rec.update(changes)
        return dict(rec)

    async def cancel_reservation(self, org_id, reservation_id):
        rec = self.reservations.get((org_id, reservation_id))
        if rec is None:
            return None
        rec["status"] = "cancelled"
        return dict(rec)

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

    async def _owned(org_id: str, reservation_id: str, name: str):
        """La reserva solo se toca si el nombre coincide: conocer el código no basta. Sin pistas de si el código existe."""
        res = await repo.get_reservation(org_id, reservation_id)
        if not res or res.get("name", "").strip().lower() != name.strip().lower():
            return None
        return res

    async def modify_reservation(a: ModifyReservationArgs, p: Principal):
        res = await _owned(p.org_id, a.reservation_id, a.name)
        if res is None:
            return {"modified": False, "reason": "no_encontrada_o_no_coincide"}
        if res.get("status") == "cancelled":
            return {"modified": False, "reason": "reserva_cancelada"}
        changes = {k: v for k, v in (("date", a.new_date), ("time", a.new_time), ("party_size", a.new_party_size)) if v is not None}
        return {"modified": True, "reservation": await repo.update_reservation(p.org_id, a.reservation_id, changes)}

    async def cancel_reservation(a: CancelReservationArgs, p: Principal):
        res = await _owned(p.org_id, a.reservation_id, a.name)
        if res is None:
            return {"cancelled": False, "reason": "no_encontrada_o_no_coincide"}
        if res.get("status") == "cancelled":
            return {"cancelled": True, "already": True}  # idempotente: repetir no cambia nada
        await repo.cancel_reservation(p.org_id, a.reservation_id)
        return {"cancelled": True, "already": False}

    async def end_call(a: EndCallArgs, p: Principal):
        return {"ending": True, "reason": a.reason}

    async def create_ticket(a: CreateTicketArgs, p: Principal):
        return await repo.create_ticket(p.org_id, a.model_dump())

    async def search_kb(a: SearchKbArgs, p: Principal):
        return {"results": await repo.search_kb(p.org_id, a.query)}

    async def transfer(a: TransferArgs, p: Principal):
        return {"transfer": "queued", "reason": a.reason}

    for t in [
        Tool("check_inventory", "Verifica existencias de un producto.", CheckInventoryArgs, "session:create", check_inventory),
        Tool("check_reservation", "Consulta una reserva por id.", CheckReservationArgs, "session:create", check_reservation),
        Tool("create_reservation", "Crea una reserva.", CreateReservationArgs, "session:create", create_reservation, mutating=True,
             needs_confirmation=True),
        Tool("modify_reservation", "Modifica fecha, hora o personas de una reserva existente (pide el nombre de la reserva).",
             ModifyReservationArgs, "session:create", modify_reservation, mutating=True, needs_confirmation=True),
        Tool("cancel_reservation", "Cancela una reserva existente (pide el nombre de la reserva).",
             CancelReservationArgs, "session:create", cancel_reservation, mutating=True, needs_confirmation=True),
        Tool("end_call", "Finaliza la llamada cuando la conversación terminó y ya te despediste.",
             EndCallArgs, "session:create", end_call, mutating=True),
        Tool("create_support_ticket", "Abre un ticket de soporte.", CreateTicketArgs, "session:create", create_ticket, mutating=True),
        Tool("search_knowledge_base", "Busca en la base de conocimiento.", SearchKbArgs, "session:create", search_kb),
        Tool("transfer_to_human", "Deriva a un operador humano.", TransferArgs, "session:create", transfer, mutating=True),
    ]:
        reg.register(t)
    return reg
