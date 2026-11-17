"use client";
import { FormEvent, useEffect, useState } from "react";
import { Agent, api, PhoneNumber } from "@/lib/api";

const E164 = /^\+[1-9][0-9]{6,14}$/;

export default function Phones() {
  const [phones, setPhones] = useState<PhoneNumber[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [number, setNumber] = useState("");
  const [agentId, setAgentId] = useState("");
  const [operator, setOperator] = useState("");

  const load = async () => {
    try {
      const [p, a] = await Promise.all([
        api<{ items: PhoneNumber[] }>("/api/v1/phone-numbers", "admin"),
        api<{ items: Agent[] }>("/api/v1/agents", "admin"),
      ]);
      setPhones(p.items);
      setAgents(a.items);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  useEffect(() => { load(); }, []);

  const wrap = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const add = (ev: FormEvent) => {
    ev.preventDefault();
    if (!E164.test(number)) return setErr("El número debe estar en formato internacional, p. ej. +56912345678");
    if (operator && !E164.test(operator)) return setErr("El operador debe estar en formato internacional");
    wrap(() =>
      api("/api/v1/phone-numbers", "admin", {
        method: "POST",
        body: JSON.stringify({ e164: number, agent_id: agentId || null, transfer_number: operator || null }),
      }),
    ).then(() => { setNumber(""); setOperator(""); setAgentId(""); });
  };

  const agentName = (id: string | null) => agents.find((a) => a.id === id)?.name ?? "Sin agente";

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Números telefónicos</h1>
      {err && <p role="alert" className="rounded bg-red-50 p-3 text-sm text-red-700">{err}</p>}
      <form onSubmit={add} className="grid gap-3 rounded border p-4 md:grid-cols-2">
        <label className="text-sm">Número
          <input value={number} onChange={(e) => setNumber(e.target.value)} placeholder="+56912345678" className="mt-1 w-full rounded border p-2" />
        </label>
        <label className="text-sm">Agente que atiende
          <select value={agentId} onChange={(e) => setAgentId(e.target.value)} className="mt-1 w-full rounded border p-2">
            <option value="">Sin agente</option>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </label>
        <label className="text-sm md:col-span-2">Operador al transferir (opcional)
          <input value={operator} onChange={(e) => setOperator(e.target.value)} placeholder="+56922223333" className="mt-1 w-full rounded border p-2" />
          <span className="text-xs text-slate-500">
            Quien edita este campo decide a qué número se desvían las llamadas: usa solo números de tu equipo.
          </span>
        </label>
        <div className="md:col-span-2">
          <button className="rounded bg-slate-900 px-4 py-2 text-sm text-white">Añadir número</button>
        </div>
      </form>
      <ul className="space-y-2">
        {phones.map((p) => (
          <li key={p.id} className="flex flex-wrap items-center justify-between gap-2 rounded border p-3 text-sm">
            <div>
              <div className="font-medium">{p.e164}</div>
              <div className="text-xs text-slate-500">{agentName(p.agent_id)} · operador {p.transfer_number ?? "global"}</div>
            </div>
            <div className="flex gap-2">
              <select
                aria-label={`Agente de ${p.e164}`}
                value={p.agent_id ?? ""}
                className="rounded border p-1 text-xs"
                onChange={(e) =>
                  wrap(() => api(`/api/v1/phone-numbers/${p.id}`, "admin", { method: "PATCH", body: JSON.stringify({ agent_id: e.target.value || null }) }))
                }
              >
                <option value="">Sin agente</option>
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              <button
                className="rounded border border-red-300 px-2 py-1 text-xs text-red-700"
                onClick={() =>
                  confirm(`¿Eliminar ${p.e164}? Las llamadas a este número dejarán de atenderse.`) &&
                  wrap(() => api(`/api/v1/phone-numbers/${p.id}`, "admin", { method: "DELETE" }))
                }
              >
                Eliminar
              </button>
            </div>
          </li>
        ))}
        {phones.length === 0 && <li className="text-sm text-slate-400">Aún no hay números.</li>}
      </ul>
    </main>
  );
}
