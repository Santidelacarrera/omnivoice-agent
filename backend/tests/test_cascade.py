"""Pipeline en cascada con STT/LLM/TTS simulados: etapas medibles, barge-in que corta de verdad, herramientas,
historial coherente y clientes HTTP/WebSocket reales contra servidores locales (sin red externa)."""
import asyncio
import json

import httpx
import pytest
import websockets

from app.core.config import Settings
from app.realtime.cascade import (AnthropicLLM, CascadedProvider, DeepgramSTT, DeepgramTTS, FRAME_BYTES,
                                  split_sentences)

S = Settings(environment="test", deepgram_api_key="dg-test-key", anthropic_api_key="an-test-key",
             provider_reconnect_backoff_s=0.01)


class FakeSTT:
    def __init__(self):
        self.q: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent = 0
        self.closed = False

    async def start(self, language): ...
    async def send(self, pcm): self.sent += len(pcm)
    async def utterances(self):
        while (t := await self.q.get()) is not None:
            yield t
    async def close(self):
        self.closed = True
        await self.q.put(None)


class FakeLLM:
    """Cada llamada a `stream` consume el siguiente guion (lista de eventos, con esperas opcionales)."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[list[dict]] = []
        self.cancelled = 0
        self.closed = False

    async def stream(self, system, messages, tools, max_tokens):
        self.calls.append(json.loads(json.dumps(messages)))
        try:
            for ev in self.scripts.pop(0):
                if isinstance(ev, (int, float)):
                    await asyncio.sleep(ev)
                elif isinstance(ev, Exception):
                    raise ev
                else:
                    yield ev
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def close(self): self.closed = True


class FakeTTS:
    def __init__(self, chunk=b"\x01\x00" * 700, delay=0.0):
        self.chunk, self.delay = chunk, delay
        self.texts: list[str] = []
        self.cancelled = 0
        self.closed = False

    async def synth(self, text, voice, language):
        self.texts.append(text)
        try:
            for _ in range(3):
                await asyncio.sleep(self.delay)
                yield self.chunk
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def close(self): self.closed = True


def text(t): return {"type": "text", "text": t}


async def collect(prov, until, timeout=3.0):
    out = []
    async def run():
        async for ev in prov.events():
            out.append(ev)
            if until(ev):
                return
    await asyncio.wait_for(run(), timeout)
    return out


def test_split_sentences():
    assert split_sentences("Hola. ¿Qué tal? Bien") == (["Hola.", "¿Qué tal?"], "Bien")
    assert split_sentences("Sin punto") == ([], "Sin punto")
    assert split_sentences("Fin.", final=True) == (["Fin."], "")
    long = "Esta es una frase bastante larga que sigue, y sigue sin ningún punto todavía"
    done, rest = split_sentences(long)
    assert done and rest and done[0].endswith(",")  # se libera en la coma para no esperar al final de la frase


async def test_turn_emits_stages_audio_frames_and_usage():
    stt, tts = FakeSTT(), FakeTTS()
    llm = FakeLLM([[text("Hola, ¿en qué te ayudo? "), text("Dime."), {"type": "usage", "in": 120, "out": 15}]])
    p = CascadedProvider(S, stt, llm, tts)
    await p.connect("sys", [], language="es")
    await stt.q.put("quiero una reserva")
    evs = await collect(p, lambda e: e["type"] == "response_done")
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "transcript_user" and evs[0]["text"] == "quiero una reserva"
    stages = {e["stage"]: e for e in evs if e["type"] == "stage"}
    assert set(stages) == {"stt_final", "llm", "tts"} and stages["llm"]["ms"] >= 0 and stages["tts"]["ms"] >= 0
    audio = [e["audio"] for e in evs if e["type"] == "audio_delta"]
    assert audio and all(len(a) == FRAME_BYTES for a in audio)  # frames de 20 ms, incluida la cola rellenada
    assert tts.texts == ["Hola, ¿en qué te ayudo?", "Dime."]  # frases sintetizadas en orden, sin esperar a toda la respuesta
    assert p.usage["llm_in_tokens"] == 120 and p.usage["llm_out_tokens"] == 15 and p.usage["tts_chars"] == 28
    await p.close()
    assert stt.closed and llm.closed and tts.closed


async def test_barge_in_cancels_llm_and_tts_for_real():
    stt, tts = FakeSTT(), FakeTTS(delay=0.05)
    llm = FakeLLM([[text("Primera frase larga. "), 5, text("nunca llega")]])
    p = CascadedProvider(S, stt, llm, tts)
    await p.connect("sys", [])
    await stt.q.put("hola")
    await collect(p, lambda e: e["type"] == "audio_delta")
    await p.cancel_response()
    assert llm.cancelled == 1  # el stream del LLM se cortó, no solo se ignoró su salida
    assert p._turn is None
    # nada más sale tras cancelar
    await asyncio.sleep(0.15)
    leftovers = []
    while not p._q.empty():
        leftovers.append(p._q.get_nowait())
    assert not [e for e in leftovers if e and e["type"] == "response_done"]
    # el turno interrumpido conserva solo lo que se llegó a decir
    assert p._history[0] == {"role": "user", "content": "hola"}
    await p.close()


async def test_tool_call_round_trip_and_history():
    stt, tts = FakeSTT(), FakeTTS()
    llm = FakeLLM([
        [text("Déjame comprobarlo. "), {"type": "tool_use", "id": "t1", "name": "check_reservation", "input": {"code": "A1"}}],
        [text("Tu reserva está confirmada.")],
    ])
    p = CascadedProvider(S, stt, llm, tts)
    await p.connect("sys", [{"name": "check_reservation", "description": "d", "parameters": {"type": "object", "properties": {}}}])
    await stt.q.put("¿mi reserva A1?")
    evs = await collect(p, lambda e: e["type"] == "tool_call")
    call = evs[-1]
    assert call["name"] == "check_reservation" and json.loads(call["arguments"]) == {"code": "A1"}
    await p.send_tool_result("t1", {"ok": True, "data": {"status": "confirmed"}})
    rest = await collect(p, lambda e: e["type"] == "response_done")
    assert any(e["type"] == "audio_delta" for e in rest)
    second_call = llm.calls[1]
    assert second_call[-1]["role"] == "user" and second_call[-1]["content"][0]["type"] == "tool_result"
    assert second_call[-2]["content"][-1]["type"] == "tool_use"
    assert [m["role"] for m in p._history] == ["user", "assistant", "user", "assistant"]
    await p.close()


async def test_interrupt_while_tool_pending_leaves_no_dangling_tool_use():
    stt, tts = FakeSTT(), FakeTTS()
    llm = FakeLLM([[{"type": "tool_use", "id": "t1", "name": "x", "input": {}}], [text("Respuesta nueva.")]])
    p = CascadedProvider(S, stt, llm, tts)
    await p.connect("sys", [])
    await stt.q.put("uno")
    await collect(p, lambda e: e["type"] == "tool_call")
    await stt.q.put("mejor otra cosa")  # el usuario habla de nuevo: el turno pendiente se cancela
    await collect(p, lambda e: e["type"] == "response_done")
    flat = json.dumps(llm.calls[1])
    assert "tool_use" not in flat and "tool_result" not in flat
    assert llm.calls[1][-1]["content"] == "mejor otra cosa"
    assert not p._tool_waiters
    await p.close()


async def test_llm_failure_is_reported_and_next_turn_works():
    stt, tts = FakeSTT(), FakeTTS()
    llm = FakeLLM([[RuntimeError("boom")], [text("Ya estoy aquí.")]])
    p = CascadedProvider(S, stt, llm, tts)
    await p.connect("sys", [])
    await stt.q.put("hola")
    evs = await collect(p, lambda e: e["type"] == "response_done")
    assert {"type": "error", "message": "turn_failed"} in evs
    await stt.q.put("¿sigues?")
    evs = await collect(p, lambda e: e["type"] == "response_done")
    assert any(e["type"] == "audio_delta" for e in evs)
    await p.close()


async def test_close_leaves_no_orphan_tasks():
    before = set(asyncio.all_tasks())
    stt, tts = FakeSTT(), FakeTTS(delay=0.05)
    llm = FakeLLM([[text("Frase uno. "), 5]])
    p = CascadedProvider(S, stt, llm, tts)
    await p.connect("sys", [])
    await stt.q.put("hola")
    await collect(p, lambda e: e["type"] == "audio_delta")
    await p.close()
    await asyncio.sleep(0.05)
    assert not (set(asyncio.all_tasks()) - before)


# ---- clientes concretos contra servidores locales ----

def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


async def test_anthropic_client_parses_text_tool_use_and_usage():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 50}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hola"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1", "name": "check_inventory"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"sku":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"X"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
    )
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"], seen["body"] = req.headers, json.loads(req.content)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    llm = AnthropicLLM(S, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    evs = [e async for e in llm.stream("sys", [{"role": "user", "content": "x"}], [{"name": "check_inventory"}], 100)]
    assert evs == [{"type": "text", "text": "Hola"},
                   {"type": "tool_use", "id": "tu1", "name": "check_inventory", "input": {"sku": "X"}},
                   {"type": "usage", "in": 50, "out": 9}]
    assert seen["headers"]["x-api-key"] == "an-test-key" and seen["body"]["stream"] is True
    await llm.close()


async def test_anthropic_client_http_error_raises():
    llm = AnthropicLLM(S, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(529))))
    with pytest.raises(httpx.HTTPStatusError):
        [e async for e in llm.stream("s", [], [], 10)]


async def test_deepgram_tts_streams_pcm_and_picks_voice_by_language():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["q"], seen["auth"] = dict(req.url.params), req.headers["authorization"]
        return httpx.Response(200, content=b"\x00\x01" * 1000)

    tts = DeepgramTTS(S, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    data = b"".join([c async for c in tts.synth("hola", None, "en-US")])
    assert len(data) == 2000
    assert seen["q"]["model"] == "aura-2-thalia-en" and seen["q"]["encoding"] == "linear16" and seen["q"]["container"] == "none"
    assert seen["auth"] == "Token dg-test-key"
    [c async for c in tts.synth("hola", "voz-inyectada", None)]
    assert seen["q"]["model"] == "aura-2-celeste-es"  # una voz fuera de la lista no se acepta
    await tts.close()


async def test_deepgram_stt_joins_fragments_and_reconnects_after_drop():
    connections = 0

    async def server(ws):
        nonlocal connections
        connections += 1
        res = lambda txt, final, sf: json.dumps({"type": "Results", "is_final": final, "speech_final": sf,
                                                 "channel": {"alternatives": [{"transcript": txt}]}})
        if connections == 1:
            await ws.send(res("quiero", True, False))
            await ws.send(res("una mesa", True, True))
            await ws.close()  # caída: el cliente debe reconectar solo
        else:
            await ws.send(res("para dos", True, True))
            await asyncio.sleep(0.2)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stt = DeepgramSTT(Settings(environment="test", deepgram_api_key="k", provider_reconnect_backoff_s=0.01,
                                   deepgram_stt_url=f"ws://127.0.0.1:{port}"))
        await stt.start("es")
        got = []
        async def run():
            async for u in stt.utterances():
                got.append(u)
                if len(got) == 2:
                    return
        await asyncio.wait_for(run(), 3)
        assert got == ["quiero una mesa", "para dos"] and connections == 2
        await stt.close()


# ---- cerebro externo (consenso multiagente) y muletilla ----

async def test_consensus_brain_client_streams_ndjson_and_ignores_progress():
    from app.realtime.cascade import ConsensusBrainLLM

    body = "\n".join(json.dumps(e) for e in [
        {"type": "progress", "stage": "debate"}, {"type": "text", "text": "Acordado."},
        {"type": "tool_use", "id": "b1", "name": "x", "input": {}}, {"type": "usage", "in": 900, "out": 40}]) + "\n"
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"], seen["body"] = req.headers.get("authorization"), json.loads(req.content)
        return httpx.Response(200, content=body)

    s = Settings(environment="test", brain_url="http://brain/converse", brain_api_key="k")
    brain = ConsensusBrainLLM(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    evs = [e async for e in brain.stream("sys", [{"role": "user", "content": "hola"}], [], 100)]
    assert [e["type"] for e in evs] == ["text", "tool_use", "usage"]
    assert seen["auth"] == "Bearer k" and seen["body"]["messages"][0]["content"] == "hola"


async def test_filler_is_spoken_when_brain_is_slow_but_not_counted_as_llm_nor_history():
    s = Settings(environment="test", filler_after_ms=50, filler_text="Un momento.")
    stt, tts = FakeSTT(), FakeTTS()
    llm = FakeLLM([[0.3, text("Respuesta final.")]])
    p = CascadedProvider(s, stt, llm, tts)
    await p.connect("sys", [])
    await stt.q.put("pregunta difícil")
    evs = await collect(p, lambda e: e["type"] == "response_done")
    assert tts.texts == ["Un momento.", "Respuesta final."]
    llm_stage = next(e for e in evs if e["type"] == "stage" and e["stage"] == "llm")
    assert llm_stage["ms"] >= 250  # la latencia real del cerebro no se disfraza
    first_audio_idx = next(i for i, e in enumerate(evs) if e["type"] == "audio_delta")
    assert first_audio_idx < evs.index(llm_stage)  # hay voz antes de que el cerebro responda
    assert p._history[-1]["content"] == [{"type": "text", "text": "Respuesta final."}]  # sin la muletilla
    await p.close()


async def test_no_filler_when_brain_answers_in_time():
    s = Settings(environment="test", filler_after_ms=500, filler_text="Un momento.")
    stt, tts = FakeSTT(), FakeTTS()
    p = CascadedProvider(s, stt, FakeLLM([[text("Rápido.")]]), tts)
    await p.connect("sys", [])
    await stt.q.put("hola")
    t0 = asyncio.get_running_loop().time()
    await collect(p, lambda e: e["type"] == "response_done")
    assert tts.texts == ["Rápido."] and asyncio.get_running_loop().time() - t0 < 0.4  # el temporizador no retrasa el turno
    await p.close()
