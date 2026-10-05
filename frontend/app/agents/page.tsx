"use client";
import { FormEvent, useEffect, useState } from "react";
import { Agent, api } from "@/lib/api";

export default function Agents() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = async () => {
    const r = await api<{ items: Agent[]; available_tools: string[] }>("/api/v1/agents", "admin");
    setAgents(r.items); setTools(r.available_tools);
  };
  useEffect(() => { load().catch((e) => setMsg({ ok: false, text: e.message })); }, []);

  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      await api("/api/v1/agents", "admin", { method: "POST", body: JSON.stringify({ name, instructions, tools: selected }) });
      setName(""); setInstructions(""); setSelected([]);
      setMsg({ ok: true, text: "Agente creado" });
      await load();
    } catch (err) { setMsg({ ok: false, text: (err as Error).message }); }
  }

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Agentes</h1>
      {msg && <p role="status" className={`rounded p-3 text-sm ${msg.ok ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{msg.text}</p>}
      <form onSubmit={create} className="space-y-3 rounded border p-4">
        <label className="block text-sm">Nombre
          <input className="mt-1 w-full rounded border p-2" value={name} onChange={(e) => setName(e.target.value)} minLength={2} maxLength={80} required />
        </label>
        <label className="block text-sm">Instrucciones
          <textarea className="mt-1 h-28 w-full rounded border p-2" value={instructions} onChange={(e) => setInstructions(e.target.value)} minLength={10} maxLength={4000} required />
        </label>
        <fieldset>
          <legend className="text-sm">Herramientas permitidas (principio de mínimo privilegio)</legend>
          <div className="mt-1 flex flex-wrap gap-3">
            {tools.map((t) => (
              <label key={t} className="flex items-center gap-1 text-sm">
                <input type="checkbox" checked={selected.includes(t)}
                  onChange={(e) => setSelected((s) => (e.target.checked ? [...s, t] : s.filter((x) => x !== t)))} />
                {t}
              </label>
            ))}
          </div>
        </fieldset>
        <button className="rounded bg-indigo-600 px-4 py-2 text-white">Crear agente</button>
      </form>
      <ul className="space-y-2">
        {agents.map((a) => (
          <li key={a.id} className="rounded border p-3">
            <div className="font-medium">{a.name} <span className="text-xs text-slate-400">({a.language})</span></div>
            <div className="text-sm text-slate-600">{a.instructions}</div>
            <div className="mt-1 text-xs text-slate-500">Herramientas: {a.tools.length ? a.tools.join(", ") : "todas"}</div>
          </li>
        ))}
        {agents.length === 0 && <li className="text-sm text-slate-400">Aún no hay agentes personalizados; se usa el agente por defecto.</li>}
      </ul>
    </main>
  );
}
