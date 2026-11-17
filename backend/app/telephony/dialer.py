"""Marcador de llamadas salientes: alta controlada de trabajos, planificador y resultado de cada intento.

Una llamada saliente es un *trabajo* (outbound_jobs) con consentimiento declarado, ventana horaria, intentos y una huella del número.
El planificador toma los trabajos vencidos (de forma atómica: varias réplicas no se pisan) y marca. El resultado de cada llamada
(ocupado, sin respuesta, fallo, completada) decide si se reintenta, se agota o termina. Ver app/telephony/outbound.py para la política.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.billing.plans import QuotaExceeded
from app.core.config import Settings
from app.observability.metrics import CALLS_ABANDONED, ERRORS, OUTBOUND_JOBS
from app.services.persistence import OutboundRejected
from app.telephony import call_state as cs
from app.telephony import outbound as ob
from app.telephony.provider import TelephonyProvider

log = structlog.get_logger()
MAX_SCHEDULE_AHEAD = timedelta(days=30)
CLAIM_BATCH = 20


class OutboundError(Exception):
    """Error de negocio con código HTTP sugerido (el mensaje es seguro de mostrar)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def policy_from_settings(s: Settings) -> ob.OutboundPolicy:
    return ob.OutboundPolicy(
        start=ob.parse_hhmm(s.outbound_window_start), end=ob.parse_hhmm(s.outbound_window_end),
        days=ob.parse_days(s.outbound_days), timezone=s.outbound_timezone, max_attempts=s.outbound_max_attempts)


def public_job(j: dict[str, Any]) -> dict[str, Any]:
    """Vista para la API: nunca el número completo."""
    n = j.get("to_number")
    nxt = j["next_attempt_at"]
    return {"id": j["id"], "status": j["status"], "attempts": j["attempts"], "max_attempts": j["max_attempts"],
            "next_attempt_at": nxt.isoformat() if hasattr(nxt, "isoformat") else nxt, "timezone": j["timezone"],
            "from_number": j["from_number"], "consent_basis": j["consent_basis"], "consent_ref": j.get("consent_ref"),
            "to_masked": (n[:3] + "•" * max(0, len(n) - 6) + n[-3:]) if n else None, "number_ref": j["number_hash"][:10],
            "last_call_sid": j.get("last_call_sid"), "last_result": j.get("last_result"),
            "created_at": j.get("created_at"), "finished_at": j.get("finished_at")}


class Dialer:
    def __init__(self, s: Settings, db_getter, store_getter, client: TelephonyProvider, voice_url: str, status_url: str, track) -> None:
        self.s, self._db, self._store, self.client = s, db_getter, store_getter, client
        self.voice_url, self.status_url, self.track = voice_url, status_url, track
        self.policy = policy_from_settings(s)
        self.plans = None  # lo asigna el arranque (app.state.plans); None = sin cuotas
        self._task: asyncio.Task | None = None

    @property
    def db(self):
        return self._db()

    @property
    def store(self):
        return self._store()

    # ---------- alta ----------
    async def pick_from_number(self, org_id: str, wanted: str | None) -> str:
        numbers = await self.db.list_phone_numbers(org_id)
        if wanted:
            if wanted not in {n["e164"] for n in numbers}:
                raise OutboundError(422, "El número de origen no pertenece a la organización")
            return wanted
        for n in numbers:
            if n["agent_id"]:
                a = await self.db.get_agent(org_id, n["agent_id"])
                if a and a["status"] == "published":
                    return n["e164"]
        if org_id == self.s.telephony_org_id and self.s.twilio_from_number:
            return self.s.twilio_from_number  # compatibilidad con la configuración global anterior
        raise OutboundError(422, "La organización no tiene un número con un agente publicado para llamar")

    async def submit(self, org_id: str, user_id: str, *, to: str, consent_basis: str, consent_ref: str | None = None,
                     scheduled_at: datetime | None = None, tz: str | None = None, max_attempts: int | None = None,
                     from_number: str | None = None, idempotency_key: str | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        try:
            ob.validate_consent(consent_basis, consent_ref)
            tz = tz or self.policy.timezone
            ob.zone(tz)  # valida
        except ob.OutboundPolicyError as exc:
            raise OutboundError(422, str(exc)) from None
        attempts = max_attempts or self.policy.max_attempts
        if not 1 <= attempts <= 10:
            raise OutboundError(422, "max_attempts debe estar entre 1 y 10")
        when = scheduled_at or now
        if when.tzinfo is None:
            raise OutboundError(422, "scheduled_at debe incluir zona horaria")
        when = when.astimezone(timezone.utc)
        if when > now + MAX_SCHEDULE_AHEAD:
            raise OutboundError(422, "No se puede programar con más de 30 días de antelación")
        when = max(when, now)
        when = ob.next_window_start(when, self.policy, tz)  # fuera de horario: pasa al próximo inicio de ventana
        if self.plans is not None:
            try:
                await self.plans.ensure_room(org_id, "max_outbound_per_day")
            except QuotaExceeded as exc:
                raise OutboundError(429, exc.message) from None
        frm = await self.pick_from_number(org_id, from_number)
        try:
            res = await self.db.create_outbound_job(
                org_id, number_hash=ob.number_hash(org_id, to), to_number=to, from_number=frm, timezone=tz,
                consent_basis=consent_basis, consent_ref=(consent_ref or "").strip() or None, idempotency_key=idempotency_key,
                max_attempts=attempts, next_attempt_at=when, created_by=user_id)
        except OutboundRejected as exc:
            OUTBOUND_JOBS.labels("rejected_" + exc.reason).inc()
            if exc.reason == "do_not_call":
                raise OutboundError(403, "El número está en la lista de exclusión") from None
            raise OutboundError(409, "Ya hay una llamada programada o en curso para ese número") from None
        OUTBOUND_JOBS.labels("created" if res["created"] else "idempotent_replay").inc()
        return res

    # ---------- planificador ----------
    async def tick(self, now: datetime | None = None) -> int:
        """Marca los trabajos vencidos. Devuelve cuántos tomó."""
        now = now or datetime.now(timezone.utc)
        jobs = await self.db.claim_due_outbound_jobs(now, CLAIM_BATCH, self.s.outbound_stale_claim_seconds)
        for job in jobs:
            await self._dial_safe(job, now)
        return len(jobs)

    async def dial_now(self, org_id: str, job_id: str, now: datetime | None = None) -> bool:
        """Marca ahora un trabajo concreto si ya toca (lo usa la petición que lo creó). No toca trabajos de otras organizaciones."""
        now = now or datetime.now(timezone.utc)
        job = await self.db.claim_outbound_job(org_id, job_id, now)
        if job is None:
            return False
        await self._dial_safe(job, now)
        return True

    async def _dial_safe(self, job: dict[str, Any], now: datetime) -> None:
        try:
            await self._dial(job, now)
        except Exception:  # noqa: BLE001 - un trabajo con fallo no debe parar a los demás
            log.exception("outbound_dial_unexpected", job_id=job["id"])
            ERRORS.labels("outbound_dial").inc()
            await self._fail(job, now)

    async def _defer(self, job: dict[str, Any], when: datetime, why: str) -> None:
        OUTBOUND_JOBS.labels("deferred_" + why).inc()
        await self.db.reschedule_outbound_job(job["org_id"], job["id"], when)

    async def _fail(self, job: dict[str, Any], now: datetime) -> None:
        await self._finish(job["org_id"], now, "FAILED", job_id=job["id"])

    async def _dial(self, job: dict[str, Any], now: datetime) -> None:
        org = job["org_id"]
        if not ob.in_window(now, self.policy, job["timezone"]):
            return await self._defer(job, ob.next_window_start(now, self.policy, job["timezone"]), "window")
        if not await self.store.hit("outbound", org, self.s.outbound_per_minute, 60) or \
                await self.store.active_sessions(org) >= self.s.max_sessions_per_org:
            return await self._defer(job, now + timedelta(seconds=60), "capacity")
        if await self.db.is_do_not_call(org, job["number_hash"]):
            OUTBOUND_JOBS.labels("blocked_do_not_call").inc()
            await self._finish(org, now, "CANCELLED", job_id=job["id"], count_attempt=False)
            return
        try:
            call = await self.client.create_call(job["to_number"], job["from_number"], self.voice_url, self.status_url)
        except Exception:  # noqa: BLE001
            log.exception("outbound_create_call_failed", job_id=job["id"])
            ERRORS.labels("telephony_outbound").inc()
            OUTBOUND_JOBS.labels("dial_failed").inc()
            return await self._fail(job, now)
        sid = call.get("sid")
        await self.db.mark_outbound_dialed(org, job["id"], sid)
        OUTBOUND_JOBS.labels("dialed").inc()
        if sid and await self.db.upsert_call(org, "twilio", sid, "outbound", None, job["from_number"], job["number_hash"][:12]):
            await self.track(org, sid, cs.QUEUED, "placed", {"job": job["id"]})

    # ---------- resultado ----------
    async def _finish(self, org_id: str, now: datetime, call_status: str, *, call_sid: str | None = None,
                      job_id: str | None = None, count_attempt: bool = True) -> dict[str, Any] | None:
        job = await self.db.finish_outbound_attempt(org_id, call_status=call_status, now=now, call_sid=call_sid, job_id=job_id,
                                                    retry_delay=lambda n: ob.retry_delay(self.policy, n))
        if job:
            OUTBOUND_JOBS.labels({"SCHEDULED": "retry_scheduled", "EXHAUSTED": "exhausted", "COMPLETED": "completed",
                                  "CANCELLED": "cancelled"}.get(job["status"], "updated")).inc()
        return job

    async def on_call_finished(self, org_id: str, call_sid: str, call_status: str) -> None:
        """Llamado cuando una llamada llega a un estado final: decide reintento, agotamiento o fin del trabajo."""
        if call_status in ob.RETRYABLE_CALL_STATES or call_status in (cs.COMPLETED, cs.TRANSFERRED, cs.CANCELLED):
            await self._finish(org_id, datetime.now(timezone.utc), call_status, call_sid=call_sid)

    # ---------- llamadas abandonadas ----------
    async def reap(self, now: datetime | None = None) -> int:
        """Cierra como FAILED las llamadas que quedaron sin final (el proveedor dejó de avisar o la réplica cayó).

        Sin esto una llamada perdida cuenta como «en curso» para siempre, bloquea el cupo de reintentos de su trabajo y ensucia la analítica.
        Pasa por `track`, así que dispara los mismos efectos que cualquier fallo: evento de webhook, resultado del trabajo saliente y métricas.
        """
        now = now or datetime.now(timezone.utc)
        stale = await self.db.find_stale_calls(now, self.s.call_stale_pre_answer_s, self.s.call_stale_live_s)
        for c in stale:
            CALLS_ABANDONED.labels(c["status"]).inc()
            if c["status"] in (cs.ANSWERED, cs.CONNECTED):
                try:  # best-effort: si la llamada seguía viva en el proveedor, se corta
                    await self.client.hangup(c["sid"])
                except Exception:  # noqa: BLE001
                    log.warning("abandoned_hangup_failed", status=c["status"])
            await self.track(c["org_id"], c["sid"], cs.FAILED, "abandoned", {"reason": "abandoned", "from": c["status"]})
        return len(stale)

    # ---------- ciclo ----------
    async def _loop(self) -> None:
        last_reap = 0.0
        while True:
            try:
                await self.tick()
                if time.monotonic() - last_reap > 300:  # cada 5 minutos
                    last_reap = time.monotonic()
                    await self.reap()
            except Exception:  # noqa: BLE001
                log.exception("outbound_tick_failed")
                ERRORS.labels("outbound_tick").inc()
            await asyncio.sleep(self.s.outbound_poll_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
