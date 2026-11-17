"""Ejecutor de evaluaciones conversacionales.

    python -m evals.runner --offline                      # valida los casos con sus transcripciones de referencia (sin red, determinista)
    ANTHROPIC_API_KEY=... python -m evals.runner          # evalúa el modelo real (gasta tokens); sale con 1 si baja del umbral
    python -m evals.runner --only medico --min-pass-rate 0.9

Un caso es un JSON en `evals/cases/` con el agente (instrucciones y herramientas), los mensajes del usuario y las comprobaciones esperadas.
`script` (opcional) es la transcripción de referencia que usa `--offline` para comprobar los propios casos y comprobaciones.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.orchestration.simulator import simulate
from evals.checks import run_checks

CASES_DIR = Path(__file__).parent / "cases"


class ScriptedLLM:
    """LLM de referencia: cada llamada consume la siguiente ronda del guion del caso (`{"text": ...}` o `{"tool": ..., "input": ...}`)."""

    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        self.rounds, self.n = [list(r) for r in rounds], 0

    async def stream(self, system, messages, tools, max_tokens):  # noqa: ANN001
        rnd = self.rounds.pop(0) if self.rounds else [{"text": "ok"}]
        for item in rnd:
            if "text" in item:
                yield {"type": "text", "text": item["text"]}
            else:
                self.n += 1
                yield {"type": "tool_use", "id": f"ev{self.n}", "name": item["tool"], "input": item["input"]}

    async def close(self) -> None:
        pass


def load_cases(directory: Path = CASES_DIR, only: str | None = None) -> list[dict[str, Any]]:
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]
    return [c for c in cases if not only or only in c["id"]]


async def run_case(settings: Settings, llm: Any, case: dict[str, Any]) -> dict[str, Any]:
    agent = {"instructions": case["agent"]["instructions"], "tools": case["agent"].get("tools", []), "language": case["agent"].get("language", "es")}
    try:
        result = await simulate(settings, llm, agent, "eval-org", case["turns"], collect_outputs=True)
    except Exception as exc:  # noqa: BLE001
        return {"id": case["id"], "passed": False, "failures": [f"la simulación falló: {exc}"], "result": None}
    failures = run_checks(result, case["checks"])
    return {"id": case["id"], "passed": not failures, "failures": failures, "result": result}


async def run_all(settings: Settings, cases: list[dict[str, Any]], offline: bool, llm: Any = None) -> list[dict[str, Any]]:
    out = []
    for case in cases:
        if offline:
            if "script" not in case:
                out.append({"id": case["id"], "passed": False, "failures": ["el caso no tiene `script` para --offline"], "result": None})
                continue
            out.append(await run_case(settings, ScriptedLLM(case["script"]), case))
        else:
            out.append(await run_case(settings, llm, case))
    return out


def summarize(results: list[dict[str, Any]]) -> tuple[float, str]:
    passed = sum(r["passed"] for r in results)
    lines = [f"{'OK ' if r['passed'] else 'FALLA'} {r['id']}" + "".join(f"\n      - {f}" for f in r["failures"]) for r in results]
    rate = passed / len(results) if results else 0.0
    return rate, "\n".join(lines + [f"\n{passed}/{len(results)} casos ({rate:.0%})"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.runner")
    ap.add_argument("--offline", action="store_true", help="usa las transcripciones de referencia, sin modelo")
    ap.add_argument("--only", help="ejecuta solo los casos cuyo id contenga este texto")
    ap.add_argument("--cases", default=str(CASES_DIR))
    ap.add_argument("--min-pass-rate", type=float, default=1.0)
    a = ap.parse_args(argv)
    settings = get_settings()
    cases = load_cases(Path(a.cases), a.only)
    if not cases:
        print("No hay casos", file=sys.stderr)
        return 2
    llm = None
    if not a.offline:
        if not settings.anthropic_api_key:
            print("Falta ANTHROPIC_API_KEY (usa --offline para validar los casos sin modelo)", file=sys.stderr)
            return 2
        from app.realtime.cascade import AnthropicLLM

        llm = AnthropicLLM(settings)
    rate, text = summarize(asyncio.run(run_all(settings, cases, a.offline, llm)))
    print(text)
    return 0 if rate >= a.min_pass_rate else 1


if __name__ == "__main__":
    raise SystemExit(main())
