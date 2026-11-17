"""Contrato del plano de control telefónico.

Todo lo que la plataforma ordena a un proveedor (marcar, desviar una llamada en curso, colgar) pasa por esta interfaz, de modo que
el marcador, la transferencia y la finalización por API no dependen de un proveedor concreto. `TwilioClient` es la implementación actual.

Alcance honesto: esto cubre solo las **órdenes salientes hacia el proveedor**. La entrada (webhook de voz, aviso de estado, formato
de la respuesta —hoy TwiML— y validación de firma) sigue siendo específica de Twilio en `routes.py`/`twilio.py`. Añadir otro proveedor
requiere, además de implementar este contrato, un adaptador de entrada propio que traduzca sus eventos a los estados de `call_state`.
"""
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TelephonyProvider(Protocol):
    async def create_call(self, to: str, from_: str, twiml_url: str, status_callback: str | None = None) -> dict[str, Any]:
        """Inicia una llamada saliente. Devuelve al menos {"sid": ..., "status": ...}."""

    async def redirect_call(self, call_sid: str, twiml: str) -> None:
        """Cambia el control de una llamada en curso (la usa la transferencia a un operador)."""

    async def hangup(self, call_sid: str, cancel: bool = False) -> None:
        """Cuelga una llamada en curso; con `cancel=True`, cancela una que aún no se contestó."""

    async def aclose(self) -> None:
        """Libera recursos de red."""
