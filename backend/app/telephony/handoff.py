"""Contexto de una transferencia a un operador humano.

Qué se entrega: motivo de la derivación + los últimos intercambios, dichos al operador (solo a él) cuando contesta,
mediante el `url` del `<Number>` de Twilio (whisper). Qué NO se hace: guardar ese texto en la base de datos. Contiene datos
personales y quedaría fuera de la política de retención, así que vive en el almacén efímero (Redis/memoria) 5 minutos, de
un solo uso, y en la base solo queda que hubo traspaso (ver ADR-2: tampoco se guardan los argumentos de las herramientas).
"""
import re

MAX_BRIEFING_CHARS = 600
HANDOFF_TTL_S = 300
_WS = re.compile(r"\s+")


def _clean(text: str, limit: int) -> str:
    t = _WS.sub(" ", text or "").strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def build_briefing(reason: str, turns: list[tuple[str, str]], source: str = "agent") -> str:
    """Texto breve para leer al operador. `turns` = [(quién, texto)] con quién en {"user", "agent"}."""
    who = {"user": "Cliente", "agent": "Agente"}
    head = "Transferencia de la llamada. " if source == "agent" else "Transferencia solicitada desde el panel. "
    parts = [head]
    if reason.strip():
        parts.append(f"Motivo: {_clean(reason, 160)}. ")
    if turns:
        parts.append("Últimos mensajes. ")
        budget = MAX_BRIEFING_CHARS - sum(len(p) for p in parts)
        lines: list[str] = []
        for speaker, text in reversed(turns):  # los más recientes primero para que no se pierdan al recortar
            line = f"{who.get(speaker, 'Cliente')}: {_clean(text, 140)}. "
            if budget - len(line) < 0:
                break
            budget -= len(line)
            lines.append(line)
        parts.extend(reversed(lines))
    return _clean("".join(parts), MAX_BRIEFING_CHARS)
