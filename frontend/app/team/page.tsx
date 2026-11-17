"use client";
import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";

interface Member { user_id: string; email: string; role: string }

const ROLES = ["owner", "admin", "manager", "operator", "analyst", "viewer"];

export default function Team() {
  const [items, setItems] = useState<Member[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("operator");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = () => api<{ items: Member[] }>("/api/v1/team", "admin").then((r) => setItems(r.items)).catch((e) => setMsg({ ok: false, text: e.message }));
  useEffect(() => { load(); }, []);

  const save = async (addr: string, newRole: string) => {
    try {
      await api(`/api/v1/team/${encodeURIComponent(addr)}`, "admin", { method: "PUT", body: JSON.stringify({ role: newRole }) });
      setMsg({ ok: true, text: `${addr}: rol ${newRole}` });
      await load();
    } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
  };
  const remove = async (addr: string) => {
    if (!confirm(`¿Quitar a ${addr} de la organización?`)) return;
    try {
      await api(`/api/v1/team/${encodeURIComponent(addr)}`, "admin", { method: "DELETE" });
      setMsg({ ok: true, text: `${addr} eliminado` });
      await load();
    } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
  };
  const add = (ev: FormEvent) => { ev.preventDefault(); if (email.trim()) { save(email.trim(), role); setEmail(""); } };

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Equipo</h1>
      <p className="text-sm text-slate-500">
        Solo un owner puede dar, quitar o cambiar el rol owner, y la organización nunca se queda sin owner. El acceso real lo concede tu
        proveedor de identidad: esta lista es la fuente que debe consultar al emitir el token.
      </p>
      {msg && <p role={msg.ok ? "status" : "alert"} className={`rounded p-3 text-sm ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{msg.text}</p>}
      <form onSubmit={add} className="flex flex-wrap items-end gap-2">
        <label className="text-sm">Email<input aria-label="Email del miembro" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="ml-2 rounded border p-1" /></label>
        <label className="text-sm">Rol
          <select aria-label="Rol" value={role} onChange={(e) => setRole(e.target.value)} className="ml-2 rounded border p-1">{ROLES.map((r) => <option key={r}>{r}</option>)}</select>
        </label>
        <button className="rounded bg-slate-900 px-3 py-1 text-sm text-white">Añadir o actualizar</button>
      </form>
      <table className="w-full text-sm">
        <thead><tr className="text-left text-slate-500"><th>Email</th><th>Rol</th><th /></tr></thead>
        <tbody>
          {items.map((m) => (
            <tr key={m.user_id} className="border-t">
              <td className="py-2 pr-3">{m.email}</td>
              <td className="pr-3">
                <select aria-label={`Rol de ${m.email}`} value={m.role} onChange={(e) => save(m.email, e.target.value)} className="rounded border p-1">
                  {ROLES.map((r) => <option key={r}>{r}</option>)}
                </select>
              </td>
              <td><button onClick={() => remove(m.email)} className="text-red-600">Quitar</button></td>
            </tr>
          ))}
          {items.length === 0 && <tr><td colSpan={3} className="py-3 text-slate-400">Sin miembros todavía.</td></tr>}
        </tbody>
      </table>
    </main>
  );
}
