"use client";
import { FormEvent, useEffect, useState } from "react";
import { api, CONSENT_LABELS, DncEntry, OutboundJob } from "@/lib/api";

const E164 = /^\+[1-9][0-9]{6,14}$/;
const ACTIVE = ["SCHEDULED", "DIALING"];

export default function Outbound() {
  const [jobs, setJobs] = useState<OutboundJob[]>([]);
  const [dnc, setDnc] = useState<DncEntry[]>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [to, setTo] = useState("");
  const [basis, setBasis] = useState("existing_customer");
  const [ref, setRef] = useState("");
  const [when, setWhen] = useState("");
  const [tz, setTz] = useState("");
  const [dncNumber, setDncNumber] = useState("");

  const load = async () => {
    try {
      const [j, d] = await Promise.all([
        api<{ items: OutboundJob[] }>("/api/v1/outbound-calls", "admin"),
        api<{ items: DncEntry[] }>("/api/v1/do-not-call", "admin"),
      ]);
      setJobs(j.items);
      setDnc(d.items);
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };
  useEffect(() => { load(); }, []);

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      setMsg({ ok: true, text: ok });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };

  const create = (ev: FormEvent) => {
    ev.preventDefault();
    if (!E164.test(to)) return setMsg({ ok: false, text: "El número debe estar en formato internacional, p. ej. +56987654321" });
    if (basis === "explicit_opt_in" && !ref.trim()) return setMsg({ ok: false, text: "Indica cómo y cuándo dio su consentimiento" });
    const body: Record<string, unknown> = { to, consent_basis: basis };
    if (ref.trim()) body.consent_ref = ref.trim();
    if (when) body.scheduled_at = new Date(when).toISOString();
    if (tz.trim()) body.timezone = tz.trim();
    run(() => api("/api/v1/outbound-calls", "admin", { method: "POST", body: JSON.stringify(body) }), "Llamada programada").then(() => setTo(""));
  };

  const addDnc = (ev: FormEvent) => {
    ev.preventDefault();
    if (!E164.test(dncNumber)) return setMsg({ ok: false, text: "Formato internacional, p. ej. +56987654321" });
    run(() => api("/api/v1/do-not-call", "admin", { method: "POST", body: JSON.stringify({ number: dncNumber, reason: "Añadido desde el panel" }) }),
      "Número excluido; lo programado para él se canceló").then(() => setDncNumber(""));
  };

  return (
    <main className="mx-auto max-w-4xl space-y-8 p-6">
      <h1 className="text-2xl font-semibold">Llamadas salientes</h1>
      {msg && <p role="status" className={`rounded p-3 text-sm ${msg.ok ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{msg.text}</p>}

      <section className="space-y-3">
        <h2 className="text-lg font-medium">Programar una llamada</h2>
        <p className="text-sm text-slate-500">
          Solo se llama dentro del horario permitido, en la zona horaria del destinatario. La plataforma registra la base de consentimiento que declares,
          pero su validez legal es responsabilidad de tu empresa y debe revisarse por país.
        </p>
        <form onSubmit={create} className="grid gap-3 rounded border p-4 md:grid-cols-2">
          <label className="text-sm">Número a llamar
            <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="+56987654321" className="mt-1 w-full rounded border p-2" />
          </label>
          <label className="text-sm">Base de consentimiento
            <select value={basis} onChange={(e) => setBasis(e.target.value)} className="mt-1 w-full rounded border p-2">
              {Object.entries(CONSENT_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          {basis === "explicit_opt_in" && (
            <label className="text-sm md:col-span-2">Cómo y cuándo consintió
              <input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="Formulario web, 2026-11-10" className="mt-1 w-full rounded border p-2" />
            </label>
          )}
          <label className="text-sm">Fecha y hora (opcional)
            <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} className="mt-1 w-full rounded border p-2" />
          </label>
          <label className="text-sm">Zona horaria del destinatario (opcional)
            <input value={tz} onChange={(e) => setTz(e.target.value)} placeholder="America/Santiago" className="mt-1 w-full rounded border p-2" />
          </label>
          <div className="md:col-span-2"><button className="rounded bg-slate-900 px-4 py-2 text-sm text-white">Programar</button></div>
        </form>
        <ul className="space-y-2">
          {jobs.map((j) => (
            <li key={j.id} className="flex flex-wrap items-center justify-between gap-2 rounded border p-3 text-sm">
              <div>
                <div className="font-medium">{j.to_masked ?? `ref. ${j.number_ref}`} <span className="ml-2 rounded bg-slate-100 px-2 py-0.5 text-xs">{j.status}</span></div>
                <div className="text-xs text-slate-500">
                  Intento {j.attempts}/{j.max_attempts} · {ACTIVE.includes(j.status) ? `próximo ${new Date(j.next_attempt_at).toLocaleString()}` : `resultado ${j.last_result ?? "—"}`}
                </div>
              </div>
              {j.status === "SCHEDULED" && (
                <button className="rounded border border-red-300 px-2 py-1 text-xs text-red-700"
                  onClick={() => run(() => api(`/api/v1/outbound-calls/${j.id}/cancel`, "admin", { method: "POST" }), "Llamada cancelada")}>Cancelar</button>
              )}
            </li>
          ))}
          {jobs.length === 0 && <li className="text-sm text-slate-400">Sin llamadas programadas.</li>}
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium">Lista de exclusión</h2>
        <p className="text-sm text-slate-500">Los números excluidos no reciben llamadas. Solo se guarda una huella del número, no el número.</p>
        <form onSubmit={addDnc} className="flex gap-2">
          <input aria-label="Número a excluir" value={dncNumber} onChange={(e) => setDncNumber(e.target.value)} placeholder="+56987654321" className="w-64 rounded border p-2 text-sm" />
          <button className="rounded border px-3 py-2 text-sm">Excluir</button>
        </form>
        <ul className="space-y-1 text-sm">
          {dnc.map((d) => (
            <li key={d.id} className="flex items-center justify-between rounded border p-2">
              <span>ref. {d.number_ref} <span className="text-xs text-slate-400">{d.reason ?? ""}</span></span>
              <button className="text-xs text-red-700 underline"
                onClick={() => confirm("¿Quitar de la lista de exclusión? Volverá a poder recibir llamadas.") && run(() => api(`/api/v1/do-not-call/${d.id}`, "admin", { method: "DELETE" }), "Quitado de la lista")}>Quitar</button>
            </li>
          ))}
          {dnc.length === 0 && <li className="text-slate-400">La lista está vacía.</li>}
        </ul>
      </section>
    </main>
  );
}
