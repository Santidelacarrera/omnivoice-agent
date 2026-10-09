"""Integración con Twilio Programmable Voice (PSTN y SIP trunking) y Media Streams.

Flujo entrante : llamada → Twilio → POST /telephony/voice (firma validada) → TwiML <Connect><Stream> → WS /ws/telephony
Flujo saliente : POST /api/v1/telephony/calls → Twilio llama → al contestar pide el mismo TwiML
Transferencia  : se redirige la llamada en curso con TwiML <Dial> hacia el operador humano.

No depende del SDK de Twilio: la API REST necesaria es mínima y así no se añade superficie de dependencias.
"""
import base64
import hashlib
import hmac
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx


def compute_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """X-Twilio-Signature: HMAC-SHA1(token, url + parámetros POST ordenados por nombre, concatenados)."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def validate_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature)


def stream_twiml(ws_url: str, parameters: dict[str, str]) -> str:
    """Conecta la llamada al WebSocket de Media Streams (bidireccional). `parameters` llegan en el evento start."""
    params = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in parameters.items())
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url={quoteattr(ws_url)}>{params}</Stream></Connect></Response>'


def dial_twiml(number: str, caller_id: str | None = None) -> str:
    cid = f" callerId={quoteattr(caller_id)}" if caller_id else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say language="es-ES">Le paso con un operador.</Say><Dial{cid}>{escape(number)}</Dial></Response>'


def hangup_twiml(message: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say language="es-ES">{escape(message)}</Say><Hangup/></Response>'


class TwilioClient:
    def __init__(self, account_sid: str, auth_token: str, client: httpx.AsyncClient | None = None) -> None:
        self.sid = account_sid
        self._auth = (account_sid, auth_token)
        self._http = client or httpx.AsyncClient(timeout=10.0)
        self._base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"

    async def create_call(self, to: str, from_: str, twiml_url: str, status_callback: str | None = None) -> dict[str, Any]:
        data = {"To": to, "From": from_, "Url": twiml_url, "Method": "POST"}
        if status_callback:
            data["StatusCallback"] = status_callback
        r = await self._http.post(f"{self._base}/Calls.json", data=data, auth=self._auth)
        r.raise_for_status()
        return r.json()

    async def redirect_call(self, call_sid: str, twiml: str) -> None:
        """Sustituye el TwiML de una llamada en curso (cierra el stream y marca al número destino)."""
        r = await self._http.post(f"{self._base}/Calls/{call_sid}.json", data={"Twiml": twiml}, auth=self._auth)
        r.raise_for_status()

    async def hangup(self, call_sid: str) -> None:
        r = await self._http.post(f"{self._base}/Calls/{call_sid}.json", data={"Status": "completed"}, auth=self._auth)
        r.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()
