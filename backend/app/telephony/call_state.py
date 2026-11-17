"""Estados de una llamada (documento maestro 8.3) y reglas de transición.

Los proveedores pueden entregar eventos repetidos, tardíos o desordenados. Regla: el estado solo avanza.
  CREATED < QUEUED < RINGING < ANSWERED < CONNECTED < (estado final)
Se admiten saltos (una entrante pasa de CREATED a CONNECTED sin QUEUED/RINGING). Cualquier estado no final puede
pasar a un final. Un estado final no cambia nunca: un `completed` de Twilio tras una transferencia no reescribe TRANSFERRED.
"""

CREATED, QUEUED, RINGING, ANSWERED, CONNECTED = "CREATED", "QUEUED", "RINGING", "ANSWERED", "CONNECTED"
COMPLETED, FAILED, BUSY, NO_ANSWER, CANCELLED, TRANSFERRED = "COMPLETED", "FAILED", "BUSY", "NO_ANSWER", "CANCELLED", "TRANSFERRED"

PROGRESS = (CREATED, QUEUED, RINGING, ANSWERED, CONNECTED)
TERMINAL = frozenset({COMPLETED, FAILED, BUSY, NO_ANSWER, CANCELLED, TRANSFERRED})
ALL_STATES = frozenset(PROGRESS) | TERMINAL

# TRANSFERRED solo desde una llamada ya atendida; el resto de finales vale desde cualquier estado no final.
_TRANSFER_FROM = frozenset({ANSWERED, CONNECTED})

TWILIO_STATUS = {
    "queued": QUEUED, "initiated": QUEUED, "ringing": RINGING, "in-progress": ANSWERED,
    "completed": COMPLETED, "busy": BUSY, "failed": FAILED, "no-answer": NO_ANSWER, "canceled": CANCELLED,
}


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def can_transition(current: str, new: str) -> bool:
    if current not in ALL_STATES or new not in ALL_STATES or current in TERMINAL:
        return False
    if new == TRANSFERRED:
        return current in _TRANSFER_FROM
    if new in TERMINAL:
        return True
    return PROGRESS.index(new) > PROGRESS.index(current)


def from_twilio(status: str) -> str | None:
    return TWILIO_STATUS.get((status or "").lower())
