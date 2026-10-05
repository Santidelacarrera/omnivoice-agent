"use client";
import { useEffect, useState } from "react";
import { api, ConversationDetail, ConversationRow } from "@/lib/api";

export default function History() {
  const [rows, setRows] = useState<ConversationRow[]>([]);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api<{ items: ConversationRow[] }>("/api/v1/conversations", "operator")
      .then((r) => setRows(r.items))
      .catch((e) => setErr(e.message));
  }, []);

  const open = (id: string) =>
    api<ConversationDetail>(`/api/v1/conversations/${id}`, "operator").then(setDetail).catch((e) => setErr(e.message));

  return (
    <main className="mx-auto grid max-w-5xl gap-6 p-6 md:grid-cols-2">
      <section>
        <h1 className="mb-3 text-2xl font-semibold">Historial</h1>
        {err && <p role="alert" className="mb-3 rounded bg-red-50 p-3 text-sm text-red-700">{err}</p>}
        <ul className="space-y-2">
          {rows.map((r) => (
            <li key={r.id}>
              <button onClick={() => open(r.id)} className="w-full rounded border p-3 text-left hover:bg-slate-50">
                <div className="text-sm">{new Date(r.created_at * 1000).toLocaleString()}</div>
                <div className="text-xs text-slate-500">
                  {r.final_state ?? "en curso"} · 1er audio {r.first_audio_ms ?? "—"} ms · {r.interruptions} interrupciones · {r.lost_packets} paquetes perdidos
                </div>
              </button>
            </li>
          ))}
          {rows.length === 0 && <li className="text-sm text-slate-400">Sin conversaciones todavía.</li>}
        </ul>
      </section>
      <section aria-label="Detalle de conversación">
        {detail ? (
          <div className="space-y-3">
            <h2 className="text-lg font-medium">Transcripción</h2>
            {detail.transcript.length === 0 && <p className="text-sm text-slate-400">No hay transcripción almacenada (según política de retención).</p>}
            {detail.transcript.map((t, i) => (
              <p key={i} className={t.speaker === "user" ? "text-slate-700" : "text-indigo-700"}>
                <b>{t.speaker === "user" ? "Cliente" : "Agente"}:</b> {t.text}
              </p>
            ))}
            <h3 className="pt-2 text-sm font-medium">Herramientas</h3>
            <ul className="text-sm text-slate-600">
              {detail.tools.map((t, i) => (
                <li key={i}>{t.tool_name ?? t.tool} · {t.ok ? "ok" : "error"} · {t.duration_ms ?? "—"} ms</li>
              ))}
            </ul>
          </div>
        ) : <p className="text-sm text-slate-400">Selecciona una conversación.</p>}
      </section>
    </main>
  );
}
