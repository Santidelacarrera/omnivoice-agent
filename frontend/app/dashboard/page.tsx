"use client";
import { useEffect, useState } from "react";
import { Analytics, api, MetricsResponse, pct, Percentiles, PlanInfo, secs } from "@/lib/api";

const fmt = (v: number | null) => (v === null ? "—" : `${v} ms`);

function PercentileCard({ title, p, target }: { title: string; p: Percentiles; target: string }) {
  return (
    <div className="rounded border p-4">
      <div className="text-xs text-slate-500">{title}</div>
      <div className="mt-1 grid grid-cols-3 gap-2 text-center">
        {(["p50", "p95", "p99"] as const).map((k) => (
          <div key={k}><div className="text-[10px] uppercase text-slate-400">{k}</div><div className="text-lg">{fmt(p[k])}</div></div>
        ))}
      </div>
      <div className="mt-2 text-[11px] text-slate-400">Objetivo: {target} · {p.count} muestras</div>
    </div>
  );
}

export default function Dashboard() {
  const [m, setM] = useState<MetricsResponse | null>(null);
  const [audit, setAudit] = useState<{ ts: number; action: string; actor: string | null; detail: Record<string, unknown> }[]>([]);
  const [an, setAn] = useState<Analytics | null>(null);
  const [plan, setPlan] = useState<PlanInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        api<PlanInfo>("/api/v1/plan", "admin").then((p) => { if (alive) setPlan(p); }).catch(() => undefined);
        const [metrics, logs, analytics] = await Promise.all([
          api<MetricsResponse>("/api/v1/metrics", "admin"),
          api<{ items: typeof audit }>("/api/v1/audit-logs?limit=25", "admin"),
          api<Analytics>("/api/v1/analytics?days=30", "admin"),
        ]);
        if (!alive) return;
        setM(metrics); setAudit(logs.items); setAn(analytics); setErr(null);
      } catch (e) { if (alive) setErr((e as Error).message); }
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  return (
    <main className="mx-auto max-w-5xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Panel de administración</h1>
      {err && <p role="alert" className="rounded bg-red-50 p-3 text-sm text-red-700">{err}</p>}
      {plan && (
        <section aria-label="Plan y consumo" className="space-y-2">
          <h2 className="text-lg font-medium">
            Plan {plan.name ?? plan.plan}
            {!plan.enforced && <span className="ml-2 text-xs font-normal text-slate-400">(cuotas solo informativas)</span>}
          </h2>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            {([["minutes_per_month", "Minutos del mes", plan.usage.minutes_month], ["max_agents", "Agentes", plan.usage.agents],
               ["max_phone_numbers", "Números", plan.usage.phone_numbers], ["max_outbound_per_day", "Salientes hoy", plan.usage.outbound_today],
               ["max_kb_sources", "Documentos", plan.usage.kb_sources]] as const).map(([k, label, used]) => {
              const limit = plan.limits[k];
              const pc = plan.percent[k];
              return (
                <div key={k} className="rounded border p-4">
                  <div className="text-xs text-slate-500">{label}</div>
                  <div className="text-xl">{used}<span className="text-sm text-slate-400"> / {limit ?? "∞"}</span></div>
                  {pc !== null && (
                    <div className="mt-1 h-1.5 w-full rounded bg-slate-100">
                      <div className={`h-1.5 rounded ${pc >= 100 ? "bg-red-500" : pc >= 80 ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${Math.min(pc, 100)}%` }} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}
      {an && (
        <section aria-label="Llamadas de los últimos 30 días" className="space-y-2">
          <h2 className="text-lg font-medium">Llamadas (30 días)</h2>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <div className="rounded border p-4"><div className="text-xs text-slate-500">Total</div><div className="text-2xl">{an.calls.total}</div></div>
            <div className="rounded border p-4"><div className="text-xs text-slate-500">Atendidas</div><div className="text-2xl">{pct(an.rates.answer_rate)}</div></div>
            <div className="rounded border p-4"><div className="text-xs text-slate-500">Perdidas</div><div className="text-2xl">{an.calls.missed}</div></div>
            <div className="rounded border p-4"><div className="text-xs text-slate-500">Transferidas</div><div className="text-2xl">{an.calls.transferred}</div></div>
            <div className="rounded border p-4"><div className="text-xs text-slate-500">Duración media</div><div className="text-2xl">{secs(an.calls.avg_duration_s)}</div></div>
          </div>
        </section>
      )}
      {m && (
        <>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
            <div className="rounded border p-4"><div className="text-xs text-slate-500">Sesiones activas</div><div className="text-2xl">{m.active_sessions}</div></div>
            <div className="rounded border p-4"><div className="text-xs text-slate-500">Audio total</div><div className="text-2xl">{Math.round(m.usage.audio_seconds / 60)} min</div></div>
            <div className="rounded border p-4"><div className="text-xs text-slate-500">Coste estimado</div><div className="text-2xl">${m.usage.estimated_cost_usd.toFixed(2)}</div></div>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <PercentileCard title="Latencia al primer audio" p={m.first_audio_ms} target="p50 < 500 ms (experimental)" />
            <PercentileCard title="Silencio tras interrupción (servidor)" p={m.barge_in_ms} target="p95 < 200 ms" />
          </div>
        </>
      )}
      <h2 className="text-lg font-medium">Auditoría reciente</h2>
      <table className="w-full text-sm">
        <thead><tr className="text-left text-slate-500"><th>Hora</th><th>Acción</th><th>Actor</th><th>Detalle</th></tr></thead>
        <tbody>
          {audit.map((e, i) => (
            <tr key={i} className="border-t align-top">
              <td className="pr-3">{new Date(e.ts * 1000).toLocaleTimeString()}</td>
              <td className="pr-3">{e.action}</td>
              <td className="pr-3">{e.actor ?? "—"}</td>
              <td className="max-w-md truncate text-slate-500">{JSON.stringify(e.detail)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
