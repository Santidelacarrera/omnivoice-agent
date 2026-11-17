"use client";
import { FormEvent, useEffect, useState } from "react";
import { api, ApiKey, Delivery, Webhook } from "@/lib/api";

const ROLES = ["viewer", "analyst", "operator", "manager", "admin"];

function OneTimeSecret({ title, value, onDone }: { title: string; value: string; onDone: () => void }) {
  return (
    <div role="alert" className="space-y-2 rounded border border-amber-300 bg-amber-50 p-3 text-sm">
      <div className="font-medium">{title}</div>
      <code className="block break-all rounded bg-white p-2">{value}</code>
      <p className="text-xs text-amber-800">Guárdala ahora: no se vuelve a mostrar. Si la pierdes, tendrás que crear o rotar otra.</p>
      <button className="rounded border px-3 py-1 text-xs" onClick={onDone}>Ya la guardé</button>
    </div>
  );
}

export default function Integrations() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [hooks, setHooks] = useState<Webhook[]>([]);
  const [events, setEvents] = useState<string[]>([]);
  const [secret, setSecret] = useState<{ title: string; value: string } | null>(null);
  const [deliveries, setDeliveries] = useState<{ hook: string; items: Delivery[] } | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [keyName, setKeyName] = useState("");
  const [keyRole, setKeyRole] = useState("viewer");
  const [url, setUrl] = useState("");

  const load = async () => {
    try {
      const [k, w] = await Promise.all([
        api<{ items: ApiKey[] }>("/api/v1/api-keys", "admin"),
        api<{ items: Webhook[]; available_events: string[] }>("/api/v1/webhooks", "admin"),
      ]);
      setKeys(k.items);
      setHooks(w.items);
      setEvents(w.available_events);
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };
  useEffect(() => { load(); }, []);

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    try {
      const r = await fn();
      if (ok) setMsg({ ok: true, text: ok });
      await load();
      return r;
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };

  const createKey = async (ev: FormEvent) => {
    ev.preventDefault();
    const r = (await run(() => api<{ key: string }>("/api/v1/api-keys", "admin", { method: "POST", body: JSON.stringify({ name: keyName, role: keyRole }) }))) as { key: string } | undefined;
    if (r) { setSecret({ title: "Clave de API (no se vuelve a mostrar)", value: r.key }); setKeyName(""); }
  };

  const createHook = async (ev: FormEvent) => {
    ev.preventDefault();
    const r = (await run(() => api<{ secret: string }>("/api/v1/webhooks", "admin", { method: "POST", body: JSON.stringify({ url }) }))) as { secret: string } | undefined;
    if (r) { setSecret({ title: "Secreto de firma del webhook (no se vuelve a mostrar)", value: r.secret }); setUrl(""); }
  };

  const rotate = async (id: string) => {
    if (!confirm("El secreto anterior dejará de valer al instante. ¿Rotar?")) return;
    const r = (await run(() => api<{ secret: string }>(`/api/v1/webhooks/${id}/rotate-secret`, "admin", { method: "POST" }))) as { secret: string } | undefined;
    if (r) setSecret({ title: "Nuevo secreto de firma (no se vuelve a mostrar)", value: r.secret });
  };

  const showDeliveries = async (id: string) => {
    const r = await api<{ items: Delivery[] }>(`/api/v1/webhooks/${id}/deliveries`, "admin").catch((e) => { setMsg({ ok: false, text: e.message }); return null; });
    if (r) setDeliveries({ hook: id, items: r.items });
  };

  return (
    <main className="mx-auto max-w-4xl space-y-8 p-6">
      <h1 className="text-2xl font-semibold">Integraciones</h1>
      {msg && <p role="status" className={`rounded p-3 text-sm ${msg.ok ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{msg.text}</p>}
      {secret && <OneTimeSecret title={secret.title} value={secret.value} onDone={() => setSecret(null)} />}

      <section className="space-y-3">
        <h2 className="text-lg font-medium">Claves de API</h2>
        <p className="text-sm text-slate-500">Para conectar tus sistemas. Una clave no puede tener más permisos que tú ni gestionar otras claves o webhooks.</p>
        <form onSubmit={createKey} className="flex flex-wrap gap-2">
          <input aria-label="Nombre de la clave" value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder="Nombre (p. ej. CRM)" minLength={2} required className="rounded border p-2 text-sm" />
          <select aria-label="Rol de la clave" value={keyRole} onChange={(e) => setKeyRole(e.target.value)} className="rounded border p-2 text-sm">
            {ROLES.map((r) => <option key={r}>{r}</option>)}
          </select>
          <button className="rounded border px-3 py-2 text-sm">Crear clave</button>
        </form>
        <ul className="space-y-1 text-sm">
          {keys.map((k) => (
            <li key={k.id} className="flex items-center justify-between rounded border p-2">
              <span>{k.name} <code className="text-xs text-slate-500">ovk_{k.prefix}…</code> · {k.role}
                {k.revoked && <span className="ml-2 rounded bg-red-100 px-2 text-xs text-red-700">revocada</span>}
                <span className="ml-2 text-xs text-slate-400">último uso {k.last_used_at ? new Date(k.last_used_at * 1000).toLocaleString() : "nunca"}</span></span>
              {!k.revoked && <button className="text-xs text-red-700 underline" onClick={() => confirm("¿Revocar esta clave? Los sistemas que la usan dejarán de funcionar.") && run(() => api(`/api/v1/api-keys/${k.id}`, "admin", { method: "DELETE" }), "Clave revocada")}>Revocar</button>}
            </li>
          ))}
          {keys.length === 0 && <li className="text-slate-400">Aún no hay claves.</li>}
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium">Webhooks</h2>
        <p className="text-sm text-slate-500">
          Recibe eventos ({events.filter((e) => e !== "*").join(", ")}). Solo https y servidores públicos. Verifica siempre la firma <code>X-OmniVoice-Signature</code> (ver la documentación de la API).
        </p>
        <form onSubmit={createHook} className="flex flex-wrap gap-2">
          <input aria-label="URL del webhook" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://tu-sistema.com/webhook" type="url" required className="w-96 rounded border p-2 text-sm" />
          <button className="rounded border px-3 py-2 text-sm">Añadir webhook</button>
        </form>
        <ul className="space-y-2 text-sm">
          {hooks.map((w) => (
            <li key={w.id} className="space-y-2 rounded border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div><div className="font-medium break-all">{w.url}</div>
                  <div className="text-xs text-slate-500">{w.events.join(", ")} · {w.active ? "activo" : "desactivado"}</div></div>
                <div className="flex flex-wrap gap-2 text-xs">
                  <button className="rounded border px-2 py-1" onClick={() => run(() => api(`/api/v1/webhooks/${w.id}/test`, "admin", { method: "POST" }), "Evento de prueba en cola")}>Probar</button>
                  <button className="rounded border px-2 py-1" onClick={() => showDeliveries(w.id)}>Entregas</button>
                  <button className="rounded border px-2 py-1" onClick={() => run(() => api(`/api/v1/webhooks/${w.id}`, "admin", { method: "PATCH", body: JSON.stringify({ active: !w.active }) }))}>{w.active ? "Desactivar" : "Activar"}</button>
                  <button className="rounded border px-2 py-1" onClick={() => rotate(w.id)}>Rotar secreto</button>
                  <button className="rounded border border-red-300 px-2 py-1 text-red-700" onClick={() => confirm("¿Eliminar el webhook y su historial de entregas?") && run(() => api(`/api/v1/webhooks/${w.id}`, "admin", { method: "DELETE" }), "Webhook eliminado")}>Eliminar</button>
                </div>
              </div>
              {deliveries?.hook === w.id && (
                <table className="w-full text-xs">
                  <thead><tr className="text-left text-slate-500"><th>Evento</th><th>Estado</th><th>Intentos</th><th>HTTP</th><th>Error</th></tr></thead>
                  <tbody>{deliveries.items.map((d) => <tr key={d.id} className="border-t"><td>{d.event_type}</td><td>{d.status}</td><td>{d.attempts}</td><td>{d.last_status ?? "—"}</td><td>{d.last_error ?? "—"}</td></tr>)}</tbody>
                </table>
              )}
            </li>
          ))}
          {hooks.length === 0 && <li className="text-slate-400">Aún no hay webhooks.</li>}
        </ul>
      </section>
    </main>
  );
}
