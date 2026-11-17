"""Etapa 5: confirmación explícita exigida por el servidor, y herramientas de modificar/cancelar/finalizar."""
import asyncio
import json
import struct
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.realtime.provider import FakeProvider
from app.security.auth import Principal
from app.tools.handlers import InMemoryRepository, build_registry
from tests.test_api import auth

LOUD = struct.pack("<480h", *([12000, -12000] * 240))
QUIET = b"\x00" * 960
ARGS = {"name": "Ana", "date": "2030-01-07", "time": "20:00", "party_size": 2}


def frame(seq, pcm):
    return struct.pack(">I", seq) + pcm


def wait(pred, tries=200):
    for _ in range(tries):
        if pred():
            return True
        time.sleep(0.02)
    return False


def tool_call(call_id, name="create_reservation", args=ARGS):
    return {"type": "tool_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}


class Harness:
    """Sesión web real con un proveedor al que el test le inyecta llamadas a herramientas en el momento exacto."""

    def __init__(self):
        self.prov = FakeProvider(audio_chunks=2, respond_on_audio=False)
        settings = Settings(environment="test", jwt_secret="t" * 40, vad_backend="energy", vad_hangover_ms=100,
                            transfer_announce_ms=10)
        self.c = TestClient(create_app(settings, provider_factory=lambda: self.prov))
        self.c.__enter__()
        self.seq = 0

    def open(self, agent_id=None):
        sid = self.c.post("/api/v1/sessions", json={"agent_id": agent_id} if agent_id else {}, headers=auth("customer")).json()["session_id"]
        ticket = self.c.post(f"/api/v1/sessions/{sid}/connect", headers=auth("customer")).json()["ticket"]
        cm = self.c.websocket_connect(f"/ws/audio?ticket={ticket}")
        self.ws = cm.__enter__()
        self.cm = cm
        return self

    def push(self, ev):
        self.c.portal.call(self.prov._q.put, ev)

    def results(self):
        return [r for _, r in self.prov.tool_results]

    def user_speaks(self):
        for _ in range(8):
            self.seq += 1
            self.ws.send_bytes(frame(self.seq, LOUD))
        for _ in range(12):
            self.seq += 1
            self.ws.send_bytes(frame(self.seq, QUIET))
        time.sleep(0.15)

    def close(self):
        self.ws.send_text('{"type":"end"}')
        self.cm.__exit__(None, None, None)


@pytest.fixture
def hz():
    h = Harness().open()
    yield h
    h.close()


def test_action_is_not_executed_until_agent_reads_back_and_user_answers(hz):
    repo = hz.c.app.state.repo
    hz.push(tool_call("c1"))
    assert wait(lambda: len(hz.results()) == 1)
    first = hz.results()[0]
    assert first["error"] == "confirmacion_requerida" and not repo.reservations  # no se ejecutó

    hz.push(tool_call("c2"))  # el modelo insiste enseguida, sin que el usuario haya hablado
    assert wait(lambda: len(hz.results()) == 2)
    assert hz.results()[1]["error"] == "confirmacion_requerida" and not repo.reservations

    hz.user_speaks()  # el usuario responde tras oír al agente
    hz.push(tool_call("c3"))
    assert wait(lambda: len(hz.results()) == 3)
    ok = hz.results()[2]
    assert ok["ok"] is True and len(repo.reservations) == 1

    hz.push(tool_call("c4"))  # la confirmación es de un solo uso
    assert wait(lambda: len(hz.results()) == 4)
    assert hz.results()[3]["error"] == "confirmacion_requerida" and len(repo.reservations) == 1


def test_user_speech_before_the_agent_spoke_does_not_count(hz):
    repo = hz.c.app.state.repo
    hz.user_speaks()  # hablaba el usuario (p. ej. su petición original), aún nadie pidió confirmar
    hz.push(tool_call("c1"))
    assert wait(lambda: len(hz.results()) == 1)
    hz.push(tool_call("c2"))
    assert wait(lambda: len(hz.results()) == 2)
    assert hz.results()[1]["error"] == "confirmacion_requerida" and not repo.reservations


def test_confirmation_is_bound_to_the_exact_arguments(hz):
    repo = hz.c.app.state.repo
    hz.push(tool_call("c1"))
    assert wait(lambda: len(hz.results()) == 1)
    hz.user_speaks()
    hz.push(tool_call("c2", args={**ARGS, "party_size": 20}))  # otro dato distinto al que se leyó
    assert wait(lambda: len(hz.results()) == 2)
    assert hz.results()[1]["error"] == "confirmacion_requerida" and not repo.reservations


def test_read_only_tools_and_ticket_do_not_need_confirmation(hz):
    hz.push(tool_call("c1", "check_inventory", {"product": "chaqueta", "color": "negro", "size": "M"}))
    assert wait(lambda: len(hz.results()) == 1)
    assert hz.results()[0]["ok"] is True


def test_end_call_notifies_the_client_after_goodbye(hz):
    hz.push(tool_call("c1", "end_call", {"reason": "completada"}))
    assert wait(lambda: len(hz.results()) == 1)
    assert hz.results()[0]["ok"] is True
    seen = []
    for _ in range(60):
        m = hz.ws.receive()
        if m.get("text"):
            seen.append(json.loads(m["text"])["type"])
            if "call.end" in seen:
                break
    assert "call.end" in seen


# ---------- herramientas: modificar / cancelar ----------
def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tools():
    repo = InMemoryRepository()
    reg = build_registry(repo)
    p = Principal("u", "o1", "customer")
    created = run(reg.execute("create_reservation", ARGS, p, "k1"))
    return repo, reg, p, created["data"]["reservation_id"]


def test_modify_reservation_requires_matching_name_and_a_change(tools):
    repo, reg, p, rid = tools
    wrong = run(reg.execute("modify_reservation", {"reservation_id": rid, "name": "Pedro", "new_time": "21:00"}, p, "m1"))
    assert wrong["data"] == {"modified": False, "reason": "no_encontrada_o_no_coincide"}
    unknown = run(reg.execute("modify_reservation", {"reservation_id": "ZZZZ9999", "name": "Ana", "new_time": "21:00"}, p, "m2"))
    assert unknown["data"] == wrong["data"]  # no revela si el código existe
    nothing = run(reg.execute("modify_reservation", {"reservation_id": rid, "name": "Ana"}, p, "m3"))
    assert nothing["error"] == "argumentos_invalidos"
    ok = run(reg.execute("modify_reservation", {"reservation_id": rid, "name": " ana ", "new_time": "21:00", "new_party_size": 4}, p, "m4"))
    assert ok["data"]["modified"] is True and repo.reservations[("o1", rid)]["time"] == "21:00" and repo.reservations[("o1", rid)]["party_size"] == 4
    assert run(reg.execute("modify_reservation", {"reservation_id": rid, "name": "Ana", "new_date": "2030-02-30"}, p, "m5"))["error"] == "argumentos_invalidos"


def test_cancel_reservation_is_idempotent_and_blocks_further_changes(tools):
    repo, reg, p, rid = tools
    assert run(reg.execute("cancel_reservation", {"reservation_id": rid, "name": "Pedro"}, p, "x1"))["data"]["cancelled"] is False
    assert run(reg.execute("cancel_reservation", {"reservation_id": rid, "name": "Ana"}, p, "x2"))["data"] == {"cancelled": True, "already": False}
    assert repo.reservations[("o1", rid)]["status"] == "cancelled"
    assert run(reg.execute("cancel_reservation", {"reservation_id": rid, "name": "Ana"}, p, "x3"))["data"]["already"] is True
    assert run(reg.execute("modify_reservation", {"reservation_id": rid, "name": "Ana", "new_time": "22:00"}, p, "x4"))["data"]["reason"] == "reserva_cancelada"


def test_reservations_are_isolated_between_orgs(tools):
    repo, reg, p, rid = tools
    other = Principal("u", "o2", "customer")
    assert run(reg.execute("cancel_reservation", {"reservation_id": rid, "name": "Ana"}, other, "y1"))["data"]["cancelled"] is False
    assert run(reg.execute("check_reservation", {"reservation_id": rid}, other, "y2"))["data"] == {"found": False}
    assert repo.reservations[("o1", rid)]["status"] == "confirmed"


def test_mutating_tools_declare_confirmation():
    reg = build_registry(InMemoryRepository())
    needs = {n for n in ("create_reservation", "modify_reservation", "cancel_reservation", "create_support_ticket",
                         "transfer_to_human", "end_call", "check_inventory") if reg.get(n).needs_confirmation}
    assert needs == {"create_reservation", "modify_reservation", "cancel_reservation"}


def test_disallowed_tool_is_refused_not_asked_to_confirm():
    h = Harness()
    try:
        agent = h.c.post("/api/v1/agents", headers=auth("admin"), json={
            "name": "Solo consulta", "instructions": "Solo consultas existencias de productos.", "tools": ["check_inventory"]}).json()
        h.open(agent["id"])
        h.push(tool_call("c1"))  # create_reservation no está permitido para este agente
        assert wait(lambda: len(h.results()) == 1)
        assert h.results()[0]["error"] == "herramienta_no_disponible"
    finally:
        h.close()
