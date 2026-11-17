"""Destinos permitidos para transferir llamadas.

Quien puede editar `transfer_number` decide a qué número se desvían las llamadas de clientes. Un número de tarificación especial o de
un país no previsto sería un fraude de coste. `TRANSFER_ALLOWED_PREFIXES` (p. ej. `+56,+34`) limita los destinos a esos prefijos
internacionales; vacío = sin límite. Se comprueba al guardar el número y otra vez al transferir (por si cambió la configuración).
`TRANSFER_BLOCKED_PREFIXES` excluye prefijos concretos (p. ej. rangos premium) incluso dentro de un prefijo permitido.
"""


def parse_prefixes(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in (value or "").split(",") if p.strip())


def is_allowed(number: str, allowed: tuple[str, ...], blocked: tuple[str, ...] = ()) -> bool:
    if not number or not number.startswith("+"):
        return False
    if any(number.startswith(b) for b in blocked):
        return False
    return not allowed or any(number.startswith(a) for a in allowed)
