"use client";
import { useEffect, useState } from "react";
import { api, MetricsResponse, Percentiles } from "@/lib/api";

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
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const [metrics, logs] = await Promise.all([
          api<MetricsResponse>("/api/v1/metrics", "admin"),
          api<{ items: typeof audit }>("/api/v1/audit-logs?limit=25", "admin"),
        ]);
        if (!alive) return;
        setM(metrics); setAudit(logs.items); setErr(null);
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
