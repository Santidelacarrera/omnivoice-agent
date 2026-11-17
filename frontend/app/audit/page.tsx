"use client";
import { useEffect, useMemo, useState } from "react";
import { api, AuditEntry } from "@/lib/api";

export default function Audit() {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api<{ items: AuditEntry[] }>("/api/v1/audit-logs?limit=1000", "admin").then((r) => setRows(r.items)).catch((e) => setErr(e.message));
  }, []);

  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    return t ? rows.filter((r) => r.action.toLowerCase().includes(t) || (r.actor ?? "").toLowerCase().includes(t)) : rows;
  }, [rows, q]);

  return (
    <main className="mx-auto max-w-5xl space-y-4 p-6">
      <h1 className="text-2xl font-semibold">Auditoría</h1>
      <p className="text-sm text-slate-500">Registro de operaciones de tu organización. Es de solo lectura: no se puede editar ni borrar.</p>
      {err && <p role="alert" className="rounded bg-red-50 p-3 text-sm text-red-700">{err}</p>}
      <input aria-label="Filtrar por acción o actor" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filtrar: webhook, agent, apikey:…" className="w-full max-w-sm rounded border p-2 text-sm" />
      <table className="w-full text-sm">
        <thead><tr className="text-left text-slate-500"><th className="pr-3">Fecha</th><th className="pr-3">Acción</th><th className="pr-3">Actor</th><th>Detalle</th></tr></thead>
        <tbody>
          {shown.map((e, i) => (
            <tr key={i} className="border-t align-top">
              <td className="whitespace-nowrap pr-3">{new Date(e.ts * 1000).toLocaleString()}</td>
              <td className="pr-3">{e.action}</td>
              <td className="pr-3">{e.actor ?? "—"}</td>
              <td className="max-w-md truncate text-slate-500" title={JSON.stringify(e.detail)}>{JSON.stringify(e.detail)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown.length === 0 && <p className="text-sm text-slate-400">Sin registros{q ? " para ese filtro" : ""}.</p>}
      <p className="text-xs text-slate-400">{shown.length} de {rows.length} registros (los 1000 más recientes).</p>
    </main>
  );
}
