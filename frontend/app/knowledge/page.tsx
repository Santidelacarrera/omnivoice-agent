"use client";
import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";

interface Source { id: string; name: string; kind: string; version: number; bytes: number; chunk_count: number; updated_at: number }
interface Hit { title: string; snippet: string; source_id?: string }

const MAX_MB = 5;

function toBase64(buf: ArrayBuffer): string {
  let bin = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export default function Knowledge() {
  const [sources, setSources] = useState<Source[]>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [replacing, setReplacing] = useState<string | null>(null);

  const load = () =>
    api<{ items: Source[] }>("/api/v1/knowledge/sources", "admin").then((r) => setSources(r.items)).catch((e) => setMsg({ ok: false, text: e.message }));
  useEffect(() => { load(); }, []);

  const upload = async (ev: ChangeEvent<HTMLInputElement>) => {
    const file = ev.target.files?.[0];
    ev.target.value = "";
    if (!file) return;
    if (file.size > MAX_MB * 1024 * 1024) return setMsg({ ok: false, text: `El archivo supera ${MAX_MB} MB` });
    setBusy(true);
    try {
      const body = JSON.stringify({ name: file.name, content_base64: toBase64(await file.arrayBuffer()), ...(file.name.toLowerCase().endsWith(".csv") ? { kind: "csv" } : {}) });
      const src = await api<Source>(replacing ? `/api/v1/knowledge/sources/${replacing}` : "/api/v1/knowledge/sources", "admin", { method: replacing ? "PUT" : "POST", body });
      setMsg({ ok: true, text: replacing ? `Documento actualizado (versión ${src.version})` : `Documento añadido: ${src.chunk_count} fragmentos` });
      setReplacing(null);
      await load();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const search = async (ev: FormEvent) => {
    ev.preventDefault();
    try {
      setHits((await api<{ results: Hit[] }>(`/api/v1/knowledge/search?q=${encodeURIComponent(q)}`, "admin")).results);
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Base de conocimiento</h1>
      <p className="text-sm text-slate-500">
        Sube documentos (PDF, DOCX, TXT o CSV, hasta {MAX_MB} MB) para que tus agentes respondan con información de tu empresa y citen la fuente. El contenido se trata como
        información, nunca como instrucciones. La búsqueda es por palabras (con raíces en español), no semántica: «devolución» y «devolver» pueden no coincidir.
      </p>
      {msg && <p role="status" className={`rounded p-3 text-sm ${msg.ok ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{msg.text}</p>}

      <label className="block rounded border border-dashed p-4 text-sm">
        {replacing ? "Elige el archivo que reemplazará al documento seleccionado" : "Añadir un documento"}
        <input type="file" accept=".pdf,.docx,.txt,.csv" disabled={busy} onChange={upload} className="mt-2 block" aria-label="Archivo" />
        {replacing && <button type="button" className="mt-2 text-xs underline" onClick={() => setReplacing(null)}>Cancelar reemplazo</button>}
      </label>

      <ul className="space-y-2">
        {sources.map((s) => (
          <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 rounded border p-3 text-sm">
            <div>
              <div className="font-medium">{s.name} <span className="ml-1 rounded bg-slate-100 px-2 text-xs uppercase">{s.kind}</span></div>
              <div className="text-xs text-slate-500">versión {s.version} · {s.chunk_count} fragmentos · {(s.bytes / 1024).toFixed(0)} KB · {new Date(s.updated_at * 1000).toLocaleString()}</div>
            </div>
            <div className="flex gap-2 text-xs">
              <button className="rounded border px-2 py-1" onClick={() => setReplacing(s.id)}>Reemplazar</button>
              <button className="rounded border border-red-300 px-2 py-1 text-red-700"
                onClick={() => confirm(`¿Eliminar «${s.name}»? Los agentes dejarán de poder citarlo.`) &&
                  api(`/api/v1/knowledge/sources/${s.id}`, "admin", { method: "DELETE" }).then(() => { setMsg({ ok: true, text: "Documento eliminado" }); return load(); }).catch((e) => setMsg({ ok: false, text: e.message }))}>
                Eliminar
              </button>
            </div>
          </li>
        ))}
        {sources.length === 0 && <li className="text-sm text-slate-400">Aún no hay documentos.</li>}
      </ul>

      <section className="space-y-2">
        <h2 className="text-lg font-medium">Probar una pregunta</h2>
        <form onSubmit={search} className="flex gap-2">
          <input aria-label="Pregunta" value={q} onChange={(e) => setQ(e.target.value)} placeholder="¿Cuál es el horario de atención?" minLength={2} required className="w-full rounded border p-2 text-sm" />
          <button className="rounded border px-3 py-2 text-sm">Buscar</button>
        </form>
        {hits && (hits.length === 0
          ? <p className="text-sm text-amber-700">Sin resultados: el agente diría que no tiene esa información y ofrecería pasar con una persona.</p>
          : <ul className="space-y-2 text-sm">{hits.map((h, i) => <li key={i} className="rounded bg-slate-50 p-3"><b>{h.title}</b><p className="mt-1 text-slate-600">{h.snippet}</p></li>)}</ul>)}
      </section>
    </main>
  );
}
