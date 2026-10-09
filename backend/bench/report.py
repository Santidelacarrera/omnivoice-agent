"""Convierte resultados de `bench.loadtest --out` en un informe Markdown con las condiciones de la medición.

Uso:  python -m bench.report resultado1.json [resultado2.json ...] > docs/LATENCY.md
El informe nunca omite si el proveedor era real o simulado: las cifras simuladas miden tu stack, no a OpenAI.
"""
import json
import sys

NOTE = {
    "real": "Proveedor real (OpenAI Realtime): incluye la latencia del modelo.",
    "simulated": "Proveedor SIMULADO: mide solo la plataforma (WebSocket, VAD, orquestación). No es la latencia que percibe un usuario.",
    "unspecified": "Modo no declarado: no interpretar como latencia de producción.",
}


def fmt(v) -> str:
    return "—" if v is None else f"{v:g}"


def render(results: list[dict]) -> str:
    out = ["# Latencia extremo a extremo bajo carga", "",
           "Generado por `bench.loadtest` + `bench.report`. Cada tabla es una medición real ejecutada por quien publica este archivo.", ""]
    for r in results:
        m = r["meta"]
        out += [f"## {m.get('label') or m['url']} — {r['clients']} clientes, {m['duration_s']:g} s ({m['timestamp']})", "",
                f"_{NOTE.get(m['provider_mode'], NOTE['unspecified'])}_", "",
                "| Métrica | muestras | p50 ms | p95 ms | p99 ms |", "|---|---|---|---|---|"]
        for name, key in (("Primer audio tras fin de turno", "first_audio_ms"), ("Barge-in (ida y vuelta)", "barge_in_roundtrip_ms")):
            d = r[key]
            out.append(f"| {name} | {d['count']} | {fmt(d['p50'])} | {fmt(d['p95'])} | {fmt(d['p99'])} |")
        out += ["", f"Conectadas: {r['connected']} · rechazadas por cupo: {r['rejected']} · timeouts: {r['timeouts']} · errores: {r['errors']}", ""]
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    print(render([json.load(open(p, encoding="utf-8")) for p in sys.argv[1:]]))
