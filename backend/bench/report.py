"""Convierte resultados de `bench.loadtest --out` en un informe Markdown con las condiciones de la medición.

Uso:  python -m bench.report resultado1.json [resultado2.json ...] > docs/LATENCY.md
El informe nunca omite si el proveedor era real o simulado: las cifras simuladas miden tu stack, no a un proveedor.
"""
import json
import sys

NOTE = {
    "real": "Proveedor REAL: incluye la latencia del modelo y de la red hasta el proveedor.",
    "simulated": "Proveedor SIMULADO: mide solo la plataforma (WebSocket, VAD, orquestación). No es la latencia que percibe un usuario.",
    "unspecified": "Modo no declarado: no interpretar como latencia de producción.",
}
STAGE_NAMES = {
    "stt": "Etapa STT (último instante de voz → transcripción final; incluye el endpointing)",
    "llm": "Etapa LLM (transcripción → primera frase lista)",
    "tts": "Etapa TTS (primera frase → primer audio)",
}


def fmt(v) -> str:
    return "—" if v is None else f"{v:g}"


def render(results: list[dict]) -> str:
    out = ["# Latencia extremo a extremo bajo carga", "",
           "Generado por `bench.loadtest` + `bench.report`. Cada tabla procede de una ejecución real de esas herramientas; "
           "el modo (real/simulado) figura siempre debajo del título.", ""]
    for r in results:
        m = r["meta"]
        title = m.get("label") or m["url"]
        out += [f"## {title} — {r['clients']} clientes, {m['duration_s']:g} s ({m['timestamp']})", "",
                f"_{NOTE.get(m['provider_mode'], NOTE['unspecified'])}_", ""]
        facts = []
        if m.get("server_provider"):
            facts.append(f"proveedor activo: `{m['server_provider']}`")
        if m.get("rtt_ms") or m.get("jitter_ms"):
            facts.append(f"red del cliente simulada: RTT {m.get('rtt_ms', 0):g} ms ± {m.get('jitter_ms', 0):g} ms")
        if m.get("seed") is not None:
            facts.append(f"semilla {m['seed']}")
        if facts:
            out += ["Condiciones: " + " · ".join(facts), ""]
        rows = [("Primer audio tras fin de turno (total)", r["first_audio_ms"]),
                ("Barge-in (ida y vuelta)", r["barge_in_roundtrip_ms"])]
        stages = r.get("stages_ms") or {}
        rows += [(STAGE_NAMES[k], stages[k]) for k in ("stt", "llm", "tts") if stages.get(k, {}).get("count")]
        out += ["| Métrica | muestras | p50 ms | p95 ms | p99 ms |", "|---|---|---|---|---|"]
        for name, d in rows:
            out.append(f"| {name} | {d['count']} | {fmt(d['p50'])} | {fmt(d['p95'])} | {fmt(d['p99'])} |")
        out += ["", f"Conectadas: {r['connected']} · rechazadas por cupo: {r['rejected']} · timeouts: {r['timeouts']} · errores: {r['errors']}"]
        c = r.get("cost")
        if c and c.get("cost_per_minute_usd") is not None:
            out.append(f"Coste estimado (consumo medido × precios configurados): **{c['cost_per_minute_usd']:.4f} USD/min** sobre "
                       f"{c['minutes']:.2f} min · desglose: " + ", ".join(f"{k} {v:.4f}" for k, v in c["by_component_usd"].items()))
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    print(render([json.load(open(p, encoding="utf-8")) for p in sys.argv[1:]]))
