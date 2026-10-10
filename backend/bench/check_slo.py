"""Compara resultados de `bench.loadtest --out` con los objetivos de `docs/slo.json`; sale con 1 si alguno se incumple.

  python -m bench.check_slo ../docs/slo.json ../docs/bench-results/b.json
Los objetivos se eligen por el modo de la medición (`simulated` / `real`) y por el nombre del escenario (`scenario`
= primera palabra de la etiqueta, p. ej. «B.»). Un objetivo sin muestras cuenta como incumplido: no se aprueba lo que no se midió.
"""
import json
import sys


def check(slo: dict, result: dict) -> list[str]:
    mode = result["meta"]["provider_mode"]
    scenario = (result["meta"].get("label") or "").split(" ")[0].rstrip(".")
    targets = (slo.get(mode) or {}).get(scenario) or (slo.get(mode) or {}).get("default") or {}
    failures = []
    for key, limits in targets.items():
        if key in ("errors", "timeouts", "rejected"):
            if result[key] > limits:
                failures.append(f"{key}: {result[key]} > {limits}")
            continue
        data = result.get(key) or (result.get("stages_ms") or {}).get(key) or {}
        for pct, limit in limits.items():
            val = data.get(pct)
            if not data.get("count") or val is None:
                failures.append(f"{key}.{pct}: sin muestras")
            elif val > limit:
                failures.append(f"{key}.{pct}: {val} ms > {limit} ms")
    return failures


if __name__ == "__main__":
    slo = json.load(open(sys.argv[1], encoding="utf-8"))
    bad = False
    for path in sys.argv[2:]:
        res = json.load(open(path, encoding="utf-8"))
        f = check(slo, res)
        print(f"{'FALLA' if f else 'OK   '} {path} [{res['meta']['provider_mode']}]")
        for line in f:
            print("   -", line)
        bad |= bool(f)
    sys.exit(1 if bad else 0)
