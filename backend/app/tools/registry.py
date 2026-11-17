"""Registro de herramientas con validación estricta, permisos, timeout e idempotencia.

El modelo NUNCA toca la base de datos: solo propone llamadas; este módulo valida
argumentos (Pydantic), comprueba permisos, ejecuta y devuelve resultados reales.
"""
import asyncio
import datetime
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.config import get_settings
from app.observability.metrics import ERRORS, TOOL_DURATION
from app.security.auth import Principal


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckInventoryArgs(ToolArgs):
    product: str = Field(min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=40)
    size: str | None = Field(default=None, max_length=10)


class CheckReservationArgs(ToolArgs):
    reservation_id: str = Field(pattern=r"^[A-Z0-9-]{4,20}$")


class CreateReservationArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=80)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    party_size: int = Field(ge=1, le=20)

    @field_validator("date")
    @classmethod
    def _real_date(cls, v: str) -> str:
        datetime.date.fromisoformat(v)  # rechaza 2030-13-45
        return v

    @field_validator("time")
    @classmethod
    def _real_time(cls, v: str) -> str:
        datetime.time.fromisoformat(v)  # rechaza 25:99
        return v


class ModifyReservationArgs(ToolArgs):
    reservation_id: str = Field(pattern=r"^[A-Z0-9-]{4,20}$")
    name: str = Field(min_length=1, max_length=80, description="Nombre con el que se hizo la reserva (verificación de identidad)")
    new_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    new_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    new_party_size: int | None = Field(default=None, ge=1, le=20)

    @field_validator("new_date")
    @classmethod
    def _real_date(cls, v: str | None) -> str | None:
        if v is not None:
            datetime.date.fromisoformat(v)
        return v

    @field_validator("new_time")
    @classmethod
    def _real_time(cls, v: str | None) -> str | None:
        if v is not None:
            datetime.time.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _something_changes(self) -> "ModifyReservationArgs":
        if self.new_date is None and self.new_time is None and self.new_party_size is None:
            raise ValueError("indica qué cambia: new_date, new_time o new_party_size")
        return self


class CancelReservationArgs(ToolArgs):
    reservation_id: str = Field(pattern=r"^[A-Z0-9-]{4,20}$")
    name: str = Field(min_length=1, max_length=80, description="Nombre con el que se hizo la reserva (verificación de identidad)")


class RegisterCustomerArgs(ToolArgs):
    first_name: str = Field(min_length=1, max_length=80, description="Nombre de pila que dice la persona")


class CheckOrderArgs(ToolArgs):
    order_id: str = Field(pattern=r"^[A-Za-z0-9-]{4,30}$")


class SendConfirmationArgs(ToolArgs):
    message: str = Field(min_length=5, max_length=160, description="Texto breve del SMS de confirmación (sin datos sensibles)")


class NoArgs(ToolArgs):
    pass


class EndCallArgs(ToolArgs):
    reason: str = Field(default="completada", min_length=2, max_length=120)


class CreateTicketArgs(ToolArgs):
    subject: str = Field(min_length=3, max_length=140)
    description: str = Field(min_length=3, max_length=2000)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high)$")


class SearchKbArgs(ToolArgs):
    query: str = Field(min_length=2, max_length=200)


class TransferArgs(ToolArgs):
    reason: str = Field(min_length=2, max_length=300)


Handler = Callable[[BaseModel, Principal], Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[ToolArgs]
    permission: str
    handler: Handler
    mutating: bool = False
    needs_confirmation: bool = False  # el servidor exige que el usuario confirme de viva voz antes de ejecutar

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._idempotency: dict[str, dict[str, Any]] = {}

    def get(self, name: str) -> "Tool | None":
        return self._tools.get(name)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        return [t.schema() for n, t in self._tools.items() if allowed is None or n in allowed]

    async def execute(
        self, name: str, raw_args: str | dict, principal: Principal, call_id: str, allowed: set[str] | None = None
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None or (allowed is not None and name not in allowed):
            ERRORS.labels("tool_unknown").inc()
            return {"ok": False, "error": "herramienta_no_disponible"}
        if not principal.can(tool.permission):
            ERRORS.labels("tool_forbidden").inc()
            return {"ok": False, "error": "permiso_denegado"}
        try:
            payload = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            args = tool.args_model.model_validate(payload)
        except (ValidationError, json.JSONDecodeError) as exc:
            ERRORS.labels("tool_invalid_args").inc()
            return {"ok": False, "error": "argumentos_invalidos", "detail": str(exc)[:300]}

        key = None
        if tool.mutating:
            key = hashlib.sha256(f"{principal.org_id}:{call_id}:{name}".encode()).hexdigest()
            if key in self._idempotency:
                return self._idempotency[key]

        start = time.monotonic()
        status = "ok"
        try:
            result = await asyncio.wait_for(tool.handler(args, principal), get_settings().tool_timeout_seconds)
            out = {"ok": True, "data": result}
        except asyncio.TimeoutError:
            status, out = "timeout", {"ok": False, "error": "timeout"}
            ERRORS.labels("tool_timeout").inc()
        except Exception:  # noqa: BLE001 - no filtrar detalles internos al modelo
            status, out = "error", {"ok": False, "error": "error_interno"}
            ERRORS.labels("tool_error").inc()
        finally:
            TOOL_DURATION.labels(name, status).observe(time.monotonic() - start)
        if key and out.get("ok"):
            self._idempotency[key] = out
        return out
