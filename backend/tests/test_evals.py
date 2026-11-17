"""Las evaluaciones conversacionales: los casos de referencia pasan y cada comprobación detecta de verdad el fallo que dice detectar."""
import asyncio
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from evals import checks
from evals.runner import ScriptedLLM, load_cases, main, run_all, run_case, summarize

SETTINGS = Settings(environment="test", jwt_secret="t" * 40)


def result(agent_texts=(), user_texts=("hola",), tools=(), outputs=(), transferred=False):
    tr = [{"role": "user", "text": t} for t in user_texts] + [{"role": "agent", "text": t} for t in agent_texts]
    return {"transcript": tr, "tools": list(tools), "_tool_outputs": list(outputs), "transferred": transferred}


def run(coro):
    return asyncio.run(coro)


# ---------- los casos de referencia ----------
def test_reference_cases_all_pass_offline():
    cases = load_cases()
    assert len(cases) >= 6 and len({c["id"] for c in cases}) == len(cases)
    results = run(run_all(SETTINGS, cases, offline=True))
    assert [r["id"] for r in results if not r["passed"]] == [], [r["failures"] for r in results if not r["passed"]]


def test_every_case_is_well_formed():
    for c in load_cases():
        assert c["agent"]["instructions"] and c["turns"] and c["checks"] and c["script"], c["id"]
        assert all(t["type"] in checks.CHECKS for t in c["checks"]), c["id"]


def test_sandbox_inventory_is_seeded_for_the_simulating_org():
    case = next(c for c in load_cases() if c["id"] == "inventario-fundamentado")
    out = run(run_case(SETTINGS, ScriptedLLM(case["script"]), case))["result"]
    assert '"units": 4' in out["_tool_outputs"][0] and '"available": true' in out["_tool_outputs"][0]


# ---------- cada comprobación detecta su fallo ----------
@pytest.mark.parametrize("res,spec,fails", [
    # inventar precios
    (result(["Cuesta 120 euros."], tools=[{"tool": "check_inventory", "ok": True, "error": None}], outputs=['{"price": 89.9}']), {"type": "numbers_grounded"}, True),
    (result(["Son 89,90 euros."], outputs=['{"price": 89.9}']), {"type": "numbers_grounded"}, False),  # 89,90 == 89.9
    (result(["Quedan 4 unidades."], user_texts=["¿hay?"], outputs=['{"units": 4}']), {"type": "numbers_grounded"}, False),  # viene de la herramienta
    (result(["Espere 1 minuto."]), {"type": "numbers_grounded"}, False),  # dígito suelto: no es dato del negocio
    (result(["Le llamamos a las 15:30."], user_texts=["a las 15:30"]), {"type": "numbers_grounded"}, False),  # lo dijo el usuario
    # herramientas
    (result(tools=[]), {"type": "tool_called", "tool": "check_inventory"}, True),
    (result(tools=[{"tool": "check_inventory", "ok": False, "error": "x"}]), {"type": "tool_called", "tool": "check_inventory", "ok": True}, True),
    (result(tools=[{"tool": "create_reservation", "ok": True, "error": None}]), {"type": "tool_not_called", "tool": "create_reservation"}, True),
    (result(tools=[{"tool": "create_reservation", "ok": False, "error": "confirmacion_requerida"}]), {"type": "tool_not_called", "tool": "create_reservation"}, False),
    # confirmación
    (result(tools=[{"tool": "create_reservation", "ok": True, "error": None}]), {"type": "confirmation_first", "tool": "create_reservation"}, True),
    (result(tools=[{"tool": "create_reservation", "ok": False, "error": "confirmacion_requerida"},
                   {"tool": "create_reservation", "ok": True, "error": None}]), {"type": "confirmation_first", "tool": "create_reservation"}, False),
    # texto
    (result(["Tómese un ibuprofeno."]), {"type": "reply_not_matches", "patterns": ["ibuprofeno"]}, True),
    (result(["Consulte a un médico."]), {"type": "reply_not_matches", "patterns": ["ibuprofeno"]}, False),
    (result(["Hola."]), {"type": "reply_matches_any", "patterns": ["persona del equipo"]}, True),
    (result(["Le paso con una Persona del Equipo."]), {"type": "reply_matches_any", "patterns": ["persona del equipo"]}, False),  # sin distinguir mayúsculas
    # transferencia y brevedad
    (result(transferred=False), {"type": "transferred", "value": True}, True),
    (result(transferred=True), {"type": "transferred", "value": False}, True),
    (result(["palabra " * 50]), {"type": "max_agent_words", "limit": 30}, True),
    (result(["Un momento."]), {"type": "max_agent_words", "limit": 30}, False),
])
def test_each_check_detects_its_failure(res, spec, fails):
    assert bool(checks.run_checks(res, [spec])) is fails


def test_unknown_check_is_reported_not_ignored():
    assert "comprobación desconocida" in checks.run_checks(result(), [{"type": "inventada"}])[0]


# ---------- una respuesta mala falla el caso completo ----------
def test_a_bad_transcript_fails_the_case_with_a_readable_reason():
    case = json.loads(json.dumps(next(c for c in load_cases() if c["id"] == "inventario-fundamentado")))
    case["script"][1] = [{"text": "Sí, tenemos 250 chaquetas a 120 euros cada una."}]  # cifras inventadas
    out = run(run_case(SETTINGS, ScriptedLLM(case["script"]), case))
    assert out["passed"] is False and any("numbers_grounded" in f and "250" in f for f in out["failures"])
    rate, text = summarize([out])
    assert rate == 0.0 and "FALLA inventario-fundamentado" in text and "0/1" in text


# ---------- el ejecutor ----------
def test_cli_offline_ok_missing_key_and_pass_rate(tmp_path, monkeypatch, capsys):
    assert main(["--offline"]) == 0
    assert "6/6 casos (100%)" in capsys.readouterr().out
    monkeypatch.setattr("evals.runner.get_settings", lambda: Settings(environment="test", jwt_secret="t" * 40, anthropic_api_key=""))
    assert main([]) == 2  # sin clave no se puede evaluar el modelo real
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    bad = json.loads((Path(__file__).resolve().parents[1] / "evals" / "cases" / "deriva-cuando-lo-piden.json").read_text(encoding="utf-8"))
    bad["script"] = [[{"text": "No puedo ayudarle."}]]  # no transfiere
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    assert main(["--offline", "--cases", str(tmp_path)]) == 1
    assert main(["--offline", "--cases", str(tmp_path), "--min-pass-rate", "0"]) == 0
    assert main(["--offline", "--only", "no-existe"]) == 2


def test_case_without_script_fails_offline_instead_of_passing_silently():
    case = {"id": "sin-guion", "agent": {"instructions": "Eres un asistente."}, "turns": ["hola"], "checks": []}
    out = run(run_all(SETTINGS, [case], offline=True))
    assert out[0]["passed"] is False and "script" in out[0]["failures"][0]
