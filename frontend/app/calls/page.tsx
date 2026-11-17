"use client";
import { useEffect, useState } from "react";
import { api, CallDetail, CallRow, secs, STATUS_STYLE } from "@/lib/api";

const FINAL = ["COMPLETED", "FAILED", "BUSY", "NO_ANSWER", "CANCELLED", "TRANSFERRED"];

function StatusChip({ status }: { status: string }) {
  return <span className={`rounded px-2 py-0.5 text-xs ${STATUS_STYLE[status] ?? "bg-slate-100 text-slate-600"}`}>{status}</span>;
}

export default function Calls() {
  const [rows, setRows] = useState<CallRow[]>([]);
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api<{ items: CallRow[] }>("/api/v1/calls", "admin").then((r) => { setRows(r.items); setErr(null); }).catch((e) => setErr(e.message));

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const open = (id: string) => api<CallDetail>(`/api/v1/calls/${id}`, "admin").then(setDetail).catch((e) => setErr(e.message));

  const act = async (kind: "transfer" | "end") => {
    if (!detail) return;
    setBusy(true);
    try {
      await api(`/api/v1/calls/${detail.id}/${kind}`, "admin", {
        method: "POST",
        body: kind === "transfer" ? JSON.stringify({ reason: "Solicitado desde el panel" }) : undefined,
      });
      await open(detail.id);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const live = detail !== null && !FINAL.includes(detail.status);

  return (
    <main className="mx-auto grid max-w-5xl gap-6 p-6 md:grid-cols-2">
      <section>
        <h1 className="mb-3 text-2xl font-semibold">Llamadas</h1>
        {err && <p role="alert" className="mb-3 rounded bg-red-50 p-3 text-sm text-red-700">{err}</p>}
        <ul className="space-y-2">
          {rows.map((r) => (
            <li key={r.id}>
              <button onClick={() => open(r.id)} className="w-full rounded border p-3 text-left hover:bg-slate-50">
                <div className="flex items-center justify-between text-sm">
                  <span>{new Date(r.created_at * 1000).toLocaleString()}</span>
                  <StatusChip status={r.status} />
                </div>
                <div className="text-xs text-slate-500">
                  {r.direction === "inbound" ? "Entrante" : "Saliente"} · línea {r.our_number ?? "—"} · duración{" "}
                  {r.answered_at && r.ended_at ? secs(r.ended_at - r.answered_at) : "—"}
                </div>
              </button>
            </li>
          ))}
          {rows.length === 0 && <li className="text-sm text-slate-400">Sin llamadas todavía.</li>}
        </ul>
      </section>
      <section aria-label="Detalle de la llamada">
        {detail ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-medium">Eventos</h2>
              <StatusChip status={detail.status} />
            </div>
            <ol className="space-y-1 text-sm">
              {detail.events.map((e, i) => (
                <li key={i} className={e.applied ? "" : "text-slate-400"}>
                  {new Date(e.ts * 1000).toLocaleTimeString()} · {e.status}
                  {e.applied ? "" : " (ignorado: llegó fuera de orden)"}
                </li>
              ))}
            </ol>
            {live && (
              <div className="flex gap-2 pt-2">
                <button disabled={busy} onClick={() => act("transfer")} className="rounded border px-3 py-1 text-sm disabled:opacity-50">
                  Transferir a un operador
                </button>
                <button disabled={busy} onClick={() => act("end")} className="rounded border border-red-300 px-3 py-1 text-sm text-red-700 disabled:opacity-50">
                  Finalizar
                </button>
              </div>
            )}
          </div>
        ) : (
          <p className="text-sm text-slate-400">Selecciona una llamada.</p>
        )}
      </section>
    </main>
  );
}
