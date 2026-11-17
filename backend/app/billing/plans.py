"""Planes y cuotas (documento maestro 18): límites por plan, consumo mensual, bloqueo al agotarse y alertas al 80 % y 100 %.

Decisiones:
- **No hay precios en el código.** Los límites de abajo son un punto de partida arbitrario, no una oferta comercial: el documento maestro
  exige fijar precios tras medir costes reales. Se sobrescriben con `PLAN_LIMITS_JSON` y, si se define `price_usd_month`, el resumen calcula
  el margen (precio menos coste estimado del proveedor de voz).
- **Opt-in.** `PLANS_ENFORCED=false` por defecto: activar cuotas en una instalación existente bloquearía organizaciones de golpe.
  Con `false` el consumo se mide y se muestra, pero nada se rechaza.
- `None` = sin límite (p. ej. Enterprise). El consumo del mes es UTC; las alertas se emiten una vez por mes y umbral.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.observability.metrics import PLAN_BLOCKED

PLAN_IDS = ("starter", "business", "professional", "enterprise")
METRICS = ("minutes_per_month", "max_agents", "max_phone_numbers", "max_outbound_per_day", "max_kb_sources")
DEFAULT_PLANS: dict[str, dict[str, Any]] = {
    "starter": {"name": "Starter", "minutes_per_month": 300, "max_agents": 2, "max_phone_numbers": 1, "max_outbound_per_day": 50,
                "max_kb_sources": 10, "price_usd_month": None},
    "business": {"name": "Business", "minutes_per_month": 1500, "max_agents": 10, "max_phone_numbers": 5, "max_outbound_per_day": 500,
                 "max_kb_sources": 50, "price_usd_month": None},
    "professional": {"name": "Professional", "minutes_per_month": 6000, "max_agents": 50, "max_phone_numbers": 25, "max_outbound_per_day": 5000,
                     "max_kb_sources": 200, "price_usd_month": None},
    "enterprise": {"name": "Enterprise", "minutes_per_month": None, "max_agents": None, "max_phone_numbers": None, "max_outbound_per_day": None,
                   "max_kb_sources": None, "price_usd_month": None},
}
ALERT_THRESHOLDS = (80, 100)


class QuotaExceeded(Exception):
    def __init__(self, metric: str, limit: int, plan: str) -> None:
        super().__init__(metric)
        self.metric, self.limit, self.plan = metric, limit, plan

    @property
    def message(self) -> str:
        labels = {"minutes_per_month": "minutos de conversación al mes", "max_agents": "agentes", "max_phone_numbers": "números telefónicos",
                  "max_outbound_per_day": "llamadas salientes al día", "max_kb_sources": "documentos de conocimiento"}
        return f"Tu plan ({self.plan}) permite hasta {self.limit} {labels.get(self.metric, self.metric)}. Actualiza el plan para continuar."


def load_plans(override_json: str = "") -> dict[str, dict[str, Any]]:
    """Planes por defecto con los cambios de `PLAN_LIMITS_JSON` (p. ej. {"starter": {"minutes_per_month": 100}}). Una clave desconocida falla al arrancar."""
    plans = {k: dict(v) for k, v in DEFAULT_PLANS.items()}
    if not override_json.strip():
        return plans
    try:
        extra = json.loads(override_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PLAN_LIMITS_JSON no es JSON válido: {exc}") from None
    if not isinstance(extra, dict):
        raise ValueError("PLAN_LIMITS_JSON debe ser un objeto {plan: {límite: valor}}")
    for plan, changes in extra.items():
        if plan not in plans or not isinstance(changes, dict):
            raise ValueError(f"PLAN_LIMITS_JSON: plan desconocido o mal formado: {plan!r}")
        for key, value in changes.items():
            if key not in METRICS and key not in ("name", "price_usd_month"):
                raise ValueError(f"PLAN_LIMITS_JSON: límite desconocido {key!r} en {plan}")
            if key in METRICS and value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"PLAN_LIMITS_JSON: {plan}.{key} debe ser un entero >= 0 o null")
            plans[plan][key] = value
    return plans


@dataclass
class Snapshot:
    plan: str
    limits: dict[str, Any]
    usage: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        pct = {}
        for metric, used in (("minutes_per_month", self.usage["minutes_month"]), ("max_agents", self.usage["agents"]),
                             ("max_phone_numbers", self.usage["phone_numbers"]), ("max_outbound_per_day", self.usage["outbound_today"]),
                             ("max_kb_sources", self.usage["kb_sources"])):
            limit = self.limits.get(metric)
            pct[metric] = round(100 * used / limit, 1) if limit else None
        price = self.limits.get("price_usd_month")
        cost = self.usage["estimated_cost_usd_month"]
        return {"plan": self.plan, "name": self.limits.get("name"), "limits": {k: self.limits.get(k) for k in METRICS},
                "usage": self.usage, "percent": pct,
                "margin": ({"price_usd": price, "estimated_cost_usd": cost, "margin_usd": round(price - cost, 2)} if price is not None else None)}


class Plans:
    def __init__(self, settings: Any, db_getter, store_getter, repo_getter, webhooks_getter) -> None:
        self.s = settings
        self.plans = load_plans(settings.plan_limits_json)
        self._db, self._store, self._repo, self._webhooks = db_getter, store_getter, repo_getter, webhooks_getter

    @property
    def enforced(self) -> bool:
        return bool(self.s.plans_enforced)

    async def plan_of(self, org_id: str) -> str:
        plan = await self._db().org_plan(org_id)
        return plan if plan in self.plans else self.s.default_plan

    async def _usage(self, org_id: str, now: datetime) -> dict[str, Any]:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        db = self._db()
        used = await db.usage_since(org_id, month_start)
        sources, _ = await self._repo().kb_totals(org_id)
        return {"minutes_month": round(used["audio_seconds"] / 60, 2), "estimated_cost_usd_month": round(used["estimated_cost_usd"], 4),
                "agents": len([a for a in await db.list_agents(org_id) if a.get("status") != "archived"]),
                "phone_numbers": len(await db.list_phone_numbers(org_id)),
                "outbound_today": await db.count_outbound_jobs_since(org_id, day_start), "kb_sources": sources}

    async def snapshot(self, org_id: str, now: datetime | None = None) -> Snapshot:
        now = now or datetime.now(timezone.utc)
        plan = await self.plan_of(org_id)
        return Snapshot(plan, self.plans[plan], await self._usage(org_id, now))

    # ---------- comprobaciones (no hacen nada si las cuotas no están activadas) ----------
    def _block(self, metric: str, limit: int, plan: str) -> QuotaExceeded:
        PLAN_BLOCKED.labels(metric).inc()
        return QuotaExceeded(metric, limit, plan)

    async def ensure_minutes(self, org_id: str) -> None:
        if not self.enforced:
            return
        snap = await self.snapshot(org_id)
        limit = snap.limits.get("minutes_per_month")
        if limit is not None and snap.usage["minutes_month"] >= limit:
            raise self._block("minutes_per_month", limit, snap.plan)

    async def ensure_room(self, org_id: str, metric: str) -> None:
        """Antes de crear un agente, número o documento: debe caber uno más."""
        if not self.enforced:
            return
        snap = await self.snapshot(org_id)
        limit = snap.limits.get(metric)
        key = {"max_agents": "agents", "max_phone_numbers": "phone_numbers", "max_kb_sources": "kb_sources", "max_outbound_per_day": "outbound_today"}[metric]
        if limit is not None and snap.usage[key] >= limit:
            raise self._block(metric, limit, snap.plan)

    # ---------- alertas ----------
    async def maybe_alert(self, org_id: str, now: datetime | None = None) -> list[int]:
        """Emite `usage.alert` al cruzar el 80 % y el 100 % de los minutos del mes (una vez por mes y umbral). Nunca lanza."""
        try:
            now = now or datetime.now(timezone.utc)
            snap = await self.snapshot(org_id, now)
            limit = snap.limits.get("minutes_per_month")
            if not limit:
                return []
            pct = 100 * snap.usage["minutes_month"] / limit
            fired = []
            for thr in ALERT_THRESHOLDS:
                if pct >= thr and await self._store().acquire_lock(f"usage-alert:{org_id}:{now:%Y-%m}:{thr}", 35 * 86400):
                    await self._webhooks().emit(org_id, "usage.alert", {"metric": "minutes_per_month", "threshold_percent": thr,
                                                                       "minutes_used": snap.usage["minutes_month"], "minutes_limit": limit, "plan": snap.plan})
                    fired.append(thr)
            return fired
        except Exception:  # noqa: BLE001 - una alerta no debe romper el cierre de una llamada
            return []

