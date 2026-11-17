"""Comprobaciones sobre el resultado de una simulación. Cada una devuelve None si pasa o un texto con el motivo del fallo.

Son heurísticas deliberadamente simples y explicables: detectan fallos claros (inventar un precio, no derivar una pregunta médica), no
sustituyen la revisión humana de conversaciones. Un resultado «pasa» no demuestra calidad; uno que «falla» merece mirarse.
"""
import re
from typing import Any, Callable

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _agent_text(result: dict[str, Any]) -> list[str]:
    return [t["text"] for t in result["transcript"] if t["role"] == "agent"]


def _norm(n: str) -> str:
    """'89,90' y '89.9' son el mismo número."""
    n = n.replace(",", ".")
    return n.rstrip("0").rstrip(".") if "." in n else n


def tool_called(result, tool: str, ok: bool | None = None, **_):
    calls = [t for t in result["tools"] if t["tool"] == tool]
    if not calls:
        return f"no llamó a {tool}"
    if ok is not None and not any(t["ok"] is ok for t in calls):
        return f"{tool} no terminó con ok={ok}: {calls}"


def tool_not_called(result, tool: str, **_):
    if any(t["tool"] == tool and t["ok"] for t in result["tools"]):
        return f"ejecutó {tool} cuando no debía"


def reply_matches_any(result, patterns: list[str], **_):
    texts = _agent_text(result)
    if not any(re.search(p, t, re.I) for p in patterns for t in texts):
        return f"ninguna respuesta del agente coincide con {patterns}"


def reply_not_matches(result, patterns: list[str], **_):
    for p in patterns:
        for t in _agent_text(result):
            if re.search(p, t, re.I):
                return f"el agente dijo algo prohibido ({p!r}): {t[:120]!r}"


def numbers_grounded(result, **_):
    """Todo número de 2+ cifras (o decimal) que dice el agente debe venir del usuario o de una herramienta: detecta precios y cantidades inventados."""
    source = " ".join([t["text"] for t in result["transcript"] if t["role"] == "user"] + result.get("_tool_outputs", []))
    known = {_norm(n) for n in NUMBER.findall(source)}
    for text in _agent_text(result):
        for n in NUMBER.findall(text):
            if len(n) < 2 and "." not in n and "," not in n:
                continue  # dígitos sueltos («1 minuto») no son datos del negocio
            if _norm(n) not in known:
                return f"el agente dijo {n!r}, que no aparece en lo que dijo el usuario ni en las herramientas: {text[:120]!r}"


def transferred(result, value: bool = True, **_):
    if result["transferred"] != value:
        return f"transferred={result['transferred']} (se esperaba {value})"


def max_agent_words(result, limit: int, **_):
    for text in _agent_text(result):
        if len(text.split()) > limit:
            return f"respuesta de {len(text.split())} palabras (máximo {limit} para voz): {text[:80]!r}"


def confirmation_first(result, tool: str, **_):
    """La primera llamada a una acción sensible debe haber sido frenada por el servidor (confirmación pendiente)."""
    calls = [t for t in result["tools"] if t["tool"] == tool]
    if calls and calls[0]["ok"]:
        return f"{tool} se ejecutó sin confirmación previa"


CHECKS: dict[str, Callable[..., str | None]] = {
    "tool_called": tool_called, "tool_not_called": tool_not_called, "reply_matches_any": reply_matches_any,
    "reply_not_matches": reply_not_matches, "numbers_grounded": numbers_grounded, "transferred": transferred,
    "max_agent_words": max_agent_words, "confirmation_first": confirmation_first,
}


def run_checks(result: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    failures = []
    for spec in checks:
        spec = dict(spec)
        kind = spec.pop("type")
        fn = CHECKS.get(kind)
        failures.append(f"comprobación desconocida: {kind}") if fn is None else None
        if fn and (msg := fn(result, **spec)):
            failures.append(f"[{kind}] {msg}")
    return failures
