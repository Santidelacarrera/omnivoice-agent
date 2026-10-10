#!/usr/bin/env bash
# Verificación y medición con proveedores REALES en un solo comando.
# Requiere DEEPGRAM_API_KEY y ANTHROPIC_API_KEY (y salida de red a api.deepgram.com / api.anthropic.com).
# Uso: cd backend && bash bench/run_real.sh [directorio_de_salida]   → deja evidencias en docs/real-results/
set -uo pipefail
OUT="${1:-../docs/real-results}"
PY="${PYTHON:-python}"
PORT=8014
: "${DEEPGRAM_API_KEY:?falta DEEPGRAM_API_KEY}" "${ANTHROPIC_API_KEY:?falta ANTHROPIC_API_KEY}"
mkdir -p "$OUT"

echo "== 1/4 Contratos con servicios reales"
"$PY" -m pytest -q -m contract tests/contract/test_cascade_contract.py 2>&1 | tee "$OUT/contract.txt"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "Los contratos fallan: corrige los clientes antes de medir (ver $OUT/contract.txt)"; exit 1; }

echo "== 2/4 Arrancando backend (cascada real, VAD por energía para la voz sintética)"
ENVIRONMENT=development PERSISTENCE_BACKEND=memory STATE_BACKEND=memory VOICE_PROVIDER=cascade VAD_BACKEND=energy \
  JWT_SECRET="$(head -c 24 /dev/urandom | base64)$(head -c 24 /dev/urandom | base64)" RETENTION_JOB_ENABLED=false \
  "$PY" -m uvicorn app.main:app --port "$PORT" --log-level warning >"$OUT/backend.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 60); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 0.5; done

echo "== 3/4 Carga real (el habla es sintética: la latencia es válida, el VAD de modelo se evalúa con voz humana)"
"$PY" -m bench.loadtest --url "http://127.0.0.1:$PORT" --mode real --clients 5 --duration 60 --seed 1 \
    --label "R1. cascada real, 5 conversaciones" --out "$OUT/r1.json" >/dev/null
"$PY" -m bench.loadtest --url "http://127.0.0.1:$PORT" --mode real --clients 5 --duration 60 --rtt-ms 200 --jitter-ms 40 --seed 1 \
    --label "R2. cascada real, 5 conversaciones, red lenta" --out "$OUT/r2.json" >/dev/null
"$PY" -m bench.report "$OUT"/r1.json "$OUT"/r2.json > "$OUT/report.md"

echo "== 4/4 Demo guionizada con voz sintetizada (timeline.json + agent.wav)"
"$PY" -m bench.demo --url "http://127.0.0.1:$PORT" --out "$OUT/demo" | tee "$OUT/demo.txt"

echo
echo "Listo. Revisa $OUT/report.md, fija los objetivos reales en docs/slo.json (p95 medido + 15 %) y verifica los PRICE_* antes de citar el coste/min."
