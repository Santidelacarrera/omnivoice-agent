"""Webhooks salientes: cola en la base de datos (outbox), firma HMAC, reintentos con espera creciente y protección SSRF.

Flujo: un evento (`call.started`, `call.completed`, `call.failed`, `call.transferred`, `tool.executed`) se **encola** en
`webhook_deliveries` para cada endpoint suscrito; un trabajador los envía. Si el proceso cae, no se pierden: siguen en la base.
La entrega es *al menos una vez*: el receptor debe deduplicar por el `id` del evento (cabecera `X-OmniVoice-Delivery` + campo `id`).

Firma: `X-OmniVoice-Signature: t=<unix>,v1=<hex>` con `v1 = HMAC-SHA256(secreto, "<t>.<cuerpo>")`. El receptor debe comprobar la firma
y que `t` sea reciente (`verify` más abajo hace ambas cosas) para frenar repeticiones.

Los eventos llevan solo identificadores, estados y duraciones: nunca números de teléfono, transcripciones ni argumentos de herramientas.
"""
import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog

from app.core.config import Settings
from app.observability.metrics import ERRORS, WEBHOOK_DELIVERIES
from app.security import netguard, secretbox

log = structlog.get_logger()

EVENT_TYPES = ("call.started", "call.completed", "call.failed", "call.transferred", "tool.executed", "usage.alert", "webhook.test")
BACKOFF_S = (30, 120, 600, 3600, 21600)  # espera tras el intento 1..5; el 6.º fallo agota la entrega
MAX_ATTEMPTS = len(BACKOFF_S) + 1
CLAIM_BATCH = 20


def sign(secret: str, timestamp: int, body: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


def signature_header(secret: str, body: str, timestamp: int | None = None) -> str:
    t = int(timestamp if timestamp is not None else time.time())
    return f"t={t},v1={sign(secret, t, body)}"


def verify(secret: str, header: str, body: str, tolerance_s: int = 300, now: float | None = None) -> bool:
    """Lo que debe hacer el receptor: firma válida y marca de tiempo dentro de la tolerancia."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - t) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, t, body), given)


def new_secret() -> str:
    return "whsec_" + uuid.uuid4().hex + uuid.uuid4().hex


class WebhookService:
    def __init__(self, s: Settings, db_getter: Callable[[], Any], http: httpx.AsyncClient | None = None,
                 resolver: netguard.Resolver = netguard.system_resolver) -> None:
        self.s, self._db, self.resolver = s, db_getter, resolver
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(s.webhook_timeout_s), follow_redirects=False)
        self._task: asyncio.Task | None = None

    @property
    def db(self):
        return self._db()

    @property
    def key_material(self) -> str:
        return self.s.webhook_secret_key or self.s.jwt_secret

    def encrypt_secret(self, secret: str) -> str:
        return secretbox.encrypt(self.key_material, secret)

    async def validate_url(self, url: str) -> None:
        """Rechaza destinos no permitidos (SSRF). Lanza `netguard.UnsafeUrl`."""
        await netguard.resolve_safe(url, self.resolver, allow_private=self.s.webhooks_allow_private,
                                    allow_http=self.s.webhooks_allow_private)

    # ---------- emisión ----------
    async def emit(self, org_id: str, event_type: str, data: dict[str, Any]) -> int:
        """Encola el evento para los endpoints suscritos. Nunca lanza: un webhook no debe romper una llamada."""
        if not self.s.webhooks_enabled:
            return 0
        try:
            event_id = str(uuid.uuid4())
            payload = {"id": event_id, "type": event_type, "created": datetime.now(timezone.utc).isoformat(),
                       "org_id": org_id, "data": data}
            return await self.db.enqueue_webhook_event(org_id, event_id, event_type, payload)
        except Exception:  # noqa: BLE001
            log.exception("webhook_enqueue_failed", event=event_type)
            ERRORS.labels("webhook_enqueue").inc()
            return 0

    # ---------- envío ----------
    async def tick(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        jobs = await self.db.claim_due_webhook_deliveries(now, CLAIM_BATCH, self.s.webhook_stale_claim_seconds)
        for job in jobs:
            try:
                await self._deliver(job, now)
            except Exception:  # noqa: BLE001 - una entrega con fallo no debe parar a las demás
                log.exception("webhook_deliver_unexpected", delivery=job["id"])
                ERRORS.labels("webhook_deliver").inc()
                await self._finish(job, now, False, None, "error_interno")
        return len(jobs)

    async def _finish(self, job: dict[str, Any], now: datetime, ok: bool, status: int | None, error: str | None,
                      retry: bool = True) -> None:
        attempt = job["attempts"] + 1
        nxt = None
        if not ok and retry and attempt < MAX_ATTEMPTS:
            nxt = now + timedelta(seconds=BACKOFF_S[attempt - 1])
        WEBHOOK_DELIVERIES.labels("delivered" if ok else "retry" if nxt else "failed").inc()
        await self.db.finish_webhook_delivery(job["org_id"], job["id"], ok, status, error, nxt)
        if not ok and nxt is None:  # entrega agotada: si el endpoint lleva una racha de fallos, se desactiva para no seguir golpeándolo
            if await self.db.disable_webhook_if_failing(job["org_id"], job["endpoint_id"], self.s.webhook_disable_after_failures):
                WEBHOOK_DELIVERIES.labels("endpoint_disabled").inc()
                await self.db.audit(job["org_id"], "system", "webhook.auto_disabled",
                                    {"webhook_id": job["endpoint_id"], "after_failures": self.s.webhook_disable_after_failures})

    async def _deliver(self, job: dict[str, Any], now: datetime) -> None:
        try:
            secret = secretbox.decrypt(self.key_material, job["secret_enc"])
        except secretbox.SecretBoxError:
            return await self._finish(job, now, False, None, "secreto_ilegible", retry=False)
        try:
            scheme, host, port, path, ip = await netguard.resolve_safe(
                job["url"], self.resolver, allow_private=self.s.webhooks_allow_private, allow_http=self.s.webhooks_allow_private)
        except netguard.UnsafeUrl as exc:
            return await self._finish(job, now, False, None, f"destino_no_permitido: {exc}"[:200])
        body = json.dumps(job["payload"], separators=(",", ":"), ensure_ascii=False)
        default_port = 443 if scheme == "https" else 80
        host_header = host if port == default_port else f"{host}:{port}"
        target = f"{scheme}://[{ip}]:{port}{path}" if ":" in ip else f"{scheme}://{ip}:{port}{path}"  # conecta a la IP ya validada
        headers = {"Content-Type": "application/json", "Host": host_header, "User-Agent": "OmniVoice-Webhooks/1",
                   "X-OmniVoice-Event": job["event_type"], "X-OmniVoice-Delivery": job["event_id"],
                   "X-OmniVoice-Signature": signature_header(secret, body)}
        try:
            r = await self.http.post(target, content=body.encode(), headers=headers, extensions={"sni_hostname": host},
                                    follow_redirects=False)  # una redirección podría llevar a una red interna
        except httpx.TimeoutException:
            return await self._finish(job, now, False, None, "timeout")
        except httpx.HTTPError as exc:
            return await self._finish(job, now, False, None, f"conexion: {type(exc).__name__}")
        if 200 <= r.status_code < 300:
            return await self._finish(job, now, True, r.status_code, None)
        await self._finish(job, now, False, r.status_code, f"http_{r.status_code}")  # no se guarda el cuerpo de la respuesta

    # ---------- mantenimiento ----------
    async def purge(self, now: datetime | None = None) -> int:
        """Borra las entregas terminadas más antiguas que `webhook_retention_days` (el historial no crece sin límite)."""
        now = now or datetime.now(timezone.utc)
        return await self.db.purge_webhook_deliveries(now - timedelta(days=self.s.webhook_retention_days))

    # ---------- ciclo ----------
    async def _loop(self) -> None:
        last_purge = 0.0
        while True:
            try:
                await self.tick()
                if time.monotonic() - last_purge > 3600:  # como mucho una vez por hora
                    last_purge = time.monotonic()
                    await self.purge()
            except Exception:  # noqa: BLE001
                log.exception("webhook_tick_failed")
                ERRORS.labels("webhook_tick").inc()
            await asyncio.sleep(self.s.webhook_poll_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.http.aclose()
