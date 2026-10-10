#!/usr/bin/env bash
# Reproduce la matriz de mediciones SIMULADAS de docs/LATENCY.md (sin claves ni red externa).
# Uso: cd backend && bash bench/run_matrix.sh [directorio_de_salida]
# Cada escenario arranca un servidor limpio (métricas y coste no se arrastran entre escenarios).
set -euo pipefail
OUT="${1:-../docs/bench-results}"
PY="${PYTHON:-python}"
PORT=8011
mkdir -p "$OUT"

scenario() {  # nombre, clientes, extra-args-loadtest...
  local name="$1" clients="$2"; shift 2
  "$PY" -m bench.sim_server --port "$PORT" --seed 1 --jitter 0.25 >/dev/null 2>&1 &
  local pid=$!
  trap 'kill $pid 2>/dev/null || true' RETURN
  for _ in $(seq 40); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 0.25; done
  "$PY" -m bench.loadtest --url "http://127.0.0.1:$PORT" --mode simulated --clients "$clients" --duration 20 --seed 1 \
      --label "$name" --out "$OUT/$(echo "$name" | cut -c1 | tr A-Z a-z).json" "$@" >/dev/null
  kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true
}

scenario "A. 1 conversación, red ideal" 1
scenario "B. 50 conversaciones simultáneas, red ideal" 50
scenario "C. 50 conversaciones, red lenta (RTT 200 ms ± 40)" 50 --rtt-ms 200 --jitter-ms 40
scenario "D. 200 conversaciones simultáneas, red ideal" 200
"$PY" -m bench.report "$OUT"/a.json "$OUT"/b.json "$OUT"/c.json "$OUT"/d.json > "$OUT/report.md"
echo "Informe en $OUT/report.md"
