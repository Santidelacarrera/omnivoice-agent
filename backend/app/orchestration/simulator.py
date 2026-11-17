"""Simulador de conversaciones por texto para probar un agente antes de publicarlo (documento maestro 10).

Usa el mismo modelo, las mismas reglas de plataforma y las mismas herramientas que una llamada real, pero:
- Las herramientas corren sobre un repositorio **en memoria desechable** (sandbox): ninguna reserva, ticket o dato real se crea ni se lee.
  Los datos que devuelven son de demostración, no de la organización: sirve para evaluar el comportamiento del agente, no sus datos.
- No toca la telefonía ni la persistencia de conversaciones, y no guarda las frases de prueba.
- Está acotado: pocos turnos, texto corto, rondas de herramientas limitadas y un plazo total.
"""
import asyncio
import json
from typing import Any

from app.core.catalog import language_directive
from app.core.config import Settings
from app.orchestration.policy import compose_instructions
from app.security.auth import Principal
from app.tools.handlers import InMemoryRepository, build_registry

MAX_TURNS = 10
MAX_TURN_CHARS = 500
MAX_TOOL_ROUNDS = 4
TOTAL_TIMEOUT_S = 60.0


class SimulationError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def validate_turns(turns: list[str]) -> list[str]:
    cleaned = [t.strip() for t in turns if isinstance(t, str) and t.strip()]
    if not cleaned:
        raise SimulationError(422, "Escribe al menos un mensaje de usuario")
    if len(cleaned) > MAX_TURNS:
        raise SimulationError(422, f"Máximo {MAX_TURNS} mensajes por simulación")
    if any(len(t) > MAX_TURN_CHARS for t in cleaned):
        raise SimulationError(422, f"Cada mensaje admite hasta {MAX_TURN_CHARS} caracteres")
    return cleaned


async def simulate(settings: Settings, llm: Any, agent: dict[str, Any], org_id: str, turns: list[str]) -> dict[str, Any]:
    """Conversa con el agente. `llm` cumple el contrato `LLMClient` del pipeline en cascada."""
    turns = validate_turns(turns)
    registry = build_registry(InMemoryRepository())  # sandbox nuevo en cada simulación
    allowed = set(agent["tools"]) if agent.get("tools") else None
    tools = [{"name": t["name"], "description": t.get("description", ""),
              "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
             for t in registry.schemas(allowed)]
    system = compose_instructions(agent["instructions"], language_directive(agent.get("language")))
    principal = Principal("simulator", org_id, "customer")
    messages: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = []
    tool_log: list[dict[str, Any]] = []
    usage = {"in": 0, "out": 0}
    call_n = 0
    turn_n = 0
    pending: dict[str, int] = {}  # confirmación exigida: la acción se ejecuta si llega un mensaje de usuario tras pedirla

    async def run() -> None:
        nonlocal call_n, turn_n
        for user_text in turns:
            turn_n += 1
            messages.append({"role": "user", "content": user_text})
            transcript.append({"role": "user", "text": user_text})
            for _ in range(MAX_TOOL_ROUNDS + 1):
                text, tool_uses = "", []
                async for ev in llm.stream(system, messages, tools, settings.cascade_max_tokens):
                    if ev["type"] == "text":
                        text += ev["text"]
                    elif ev["type"] == "tool_use":
                        tool_uses.append(ev)
                    elif ev["type"] == "usage":
                        usage["in"] += int(ev.get("in", 0))
                        usage["out"] += int(ev.get("out", 0))
                blocks: list[dict[str, Any]] = ([{"type": "text", "text": text}] if text else []) + [
                    {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["input"]} for t in tool_uses]
                if blocks:
                    messages.append({"role": "assistant", "content": blocks})
                if text:
                    transcript.append({"role": "agent", "text": text})
                if not tool_uses:
                    break
                results = []
                for tu in tool_uses:
                    call_n += 1
                    tool = registry.get(tu["name"])
                    key = tu["name"] + ":" + json.dumps(tu["input"], sort_keys=True, ensure_ascii=False)
                    permitted = allowed is None or tu["name"] in allowed
                    if tool is not None and permitted and tool.needs_confirmation and not (key in pending and turn_n > pending[key]):
                        pending.setdefault(key, turn_n)
                        out = {"ok": False, "error": "confirmacion_requerida",
                               "detail": "Lee los datos al usuario, espera su respuesta y, solo si confirma, repite la llamada idéntica."}
                    else:
                        pending.pop(key, None)
                        out = await registry.execute(tu["name"], tu["input"], principal, f"sim-{call_n}", allowed)
                    tool_log.append({"tool": tu["name"], "ok": bool(out.get("ok")), "error": out.get("error")})
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": json.dumps(out, ensure_ascii=False)})
                messages.append({"role": "user", "content": results})
            else:
                transcript.append({"role": "system", "text": "Se alcanzó el máximo de rondas de herramientas en este turno."})

    try:
        await asyncio.wait_for(run(), TOTAL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise SimulationError(504, "La simulación tardó demasiado") from None
    except SimulationError:
        raise
    except Exception:  # noqa: BLE001 - el detalle del proveedor no se expone
        raise SimulationError(502, "El modelo no respondió; inténtalo de nuevo") from None
    return {"sandbox": True, "note": "Las herramientas usaron datos de demostración: no se leyó ni se cambió ningún dato real.",
            "transcript": transcript, "tools": tool_log, "usage": usage,
            "transferred": any(t["tool"] == "transfer_to_human" and t["ok"] for t in tool_log)}
