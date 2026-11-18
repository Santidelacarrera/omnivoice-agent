"use client";
import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";

interface Connector {
  id: string; name: string; base_url: string; auth_type: string; allowed_methods: string[]; allowed_paths: string[];
  enabled: boolean; has_credential: boolean;
}

const AUTH_LABEL: Record<string, string> = {
  none: "Sin autenticación", bearer: "Token (Bearer)", header: "Cabecera personalizada", basic: "Usuario y contraseña",
  oauth2_client_credentials: "OAuth2 (client credentials)",
};

export default function Connectors() {
  const [items, setItems] = useState<Connector[]>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [f, setF] = useState({ name: "", base_url: "", auth_type: "bearer", token: "", header_name: "", header_value: "", username: "", password: "",
    client_id: "", client_secret: "", token_url: "", paths: "/", write: false });

  const load = () => api<{ items: Connector[] }>("/api/v1/integrations", "admin").then((r) => setItems(r.items)).catch((e) => setMsg({ ok: false, text: e.message }));
  useEffect(() => { load(); }, []);
  const set = (k: string, v: string | boolean) => setF((p) => ({ ...p, [k]: v }));

  const credential = () => {
    switch (f.auth_type) {
      case "bearer": return { token: f.token };
      case "header": return { value: f.header_value };
      case "basic": return { username: f.username, password: f.password };
      case "oauth2_client_credentials": return { client_id: f.client_id, client_secret: f.client_secret };
      default: return undefined;
    }
  };

  const create = async (ev: FormEvent) => {
    ev.preventDefault();
    try {
      await api("/api/v1/integrations", "admin", { method: "POST", body: JSON.stringify({
        name: f.name, base_url: f.base_url, auth_type: f.auth_type, credential: credential(),
        auth_header: f.auth_type === "header" ? f.header_name : undefined, token_url: f.auth_type === "oauth2_client_credentials" ? f.token_url : undefined,
        methods: f.write ? ["GET", "POST", "PUT", "PATCH", "DELETE"] : ["GET"], paths: f.paths.split(",").map((p) => p.trim()).filter(Boolean),
      }) });
      setMsg({ ok: true, text: `Conector «${f.name}» creado` });
      setF((p) => ({ ...p, name: "", token: "", header_value: "", password: "", client_secret: "" }));
      await load();
    } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
  };

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try { await fn(); setMsg({ ok: true, text: ok }); await load(); } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
  };
  const test = (c: Connector) => act(async () => {
    const r = await api<{ status: number }>(`/api/v1/integrations/${c.name}/test`, "admin", { method: "POST", body: JSON.stringify({ path: c.allowed_paths[0] ?? "/" }) });
    if (r.status >= 400) throw new Error(`${c.name}: el servicio respondió HTTP ${r.status}`);
  }, `${c.name}: conexión correcta`);

  return (
    <main className="mx-auto max-w-4xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Conectores</h1>
      <p className="text-sm text-slate-500">
        Conecta sistemas propios por HTTPS (CRM, agenda, ERP). El agente solo puede llamar a los métodos y rutas que permitas aquí, y las
        credenciales se guardan cifradas y no se vuelven a mostrar. Para usarlos, activa las herramientas <code>integration_read</code> y
        <code> integration_write</code> en el agente. No están verificados contra servicios de terceros concretos ni admiten OAuth con
        consentimiento de usuario (Google, Outlook).
      </p>
      {msg && <p role={msg.ok ? "status" : "alert"} className={`rounded p-3 text-sm ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{msg.text}</p>}

      <form onSubmit={create} className="grid gap-3 rounded border p-4 md:grid-cols-2" aria-label="Nuevo conector">
        <label className="text-sm">Nombre<input aria-label="Nombre del conector" required value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="crm" className="mt-1 w-full rounded border p-1" /></label>
        <label className="text-sm">URL base (https)<input aria-label="URL base" required value={f.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder="https://crm.empresa.com/api" className="mt-1 w-full rounded border p-1" /></label>
        <label className="text-sm">Autenticación
          <select aria-label="Autenticación" value={f.auth_type} onChange={(e) => set("auth_type", e.target.value)} className="mt-1 w-full rounded border p-1">
            {Object.entries(AUTH_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="text-sm">Rutas permitidas (separadas por coma)<input aria-label="Rutas permitidas" value={f.paths} onChange={(e) => set("paths", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>
        {f.auth_type === "bearer" && <label className="text-sm">Token<input aria-label="Token" type="password" value={f.token} onChange={(e) => set("token", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>}
        {f.auth_type === "header" && <>
          <label className="text-sm">Nombre de la cabecera<input aria-label="Nombre de la cabecera" value={f.header_name} onChange={(e) => set("header_name", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>
          <label className="text-sm">Valor<input aria-label="Valor de la cabecera" type="password" value={f.header_value} onChange={(e) => set("header_value", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>
        </>}
        {f.auth_type === "basic" && <>
          <label className="text-sm">Usuario<input aria-label="Usuario" value={f.username} onChange={(e) => set("username", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>
          <label className="text-sm">Contraseña<input aria-label="Contraseña" type="password" value={f.password} onChange={(e) => set("password", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>
        </>}
        {f.auth_type === "oauth2_client_credentials" && <>
          <label className="text-sm">URL del token<input aria-label="URL del token" value={f.token_url} onChange={(e) => set("token_url", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>
          <label className="text-sm">Client ID<input aria-label="Client ID" value={f.client_id} onChange={(e) => set("client_id", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>
          <label className="text-sm">Client secret<input aria-label="Client secret" type="password" value={f.client_secret} onChange={(e) => set("client_secret", e.target.value)} className="mt-1 w-full rounded border p-1" /></label>
        </>}
        <label className="flex items-center gap-2 text-sm md:col-span-2"><input type="checkbox" checked={f.write} onChange={(e) => set("write", e.target.checked)} />Permitir también escribir (POST, PUT, PATCH, DELETE; el agente pedirá confirmación al usuario)</label>
        <button className="rounded bg-slate-900 px-3 py-2 text-sm text-white md:col-span-2">Crear conector</button>
      </form>

      <table className="w-full text-sm">
        <thead><tr className="text-left text-slate-500"><th>Nombre</th><th>Destino</th><th>Acceso</th><th /></tr></thead>
        <tbody>
          {items.map((c) => (
            <tr key={c.id} className="border-t align-top">
              <td className="py-2 pr-3">{c.name}{!c.enabled && <span className="ml-2 text-xs text-slate-400">(desactivado)</span>}</td>
              <td className="pr-3"><div>{c.base_url}</div><div className="text-xs text-slate-400">{AUTH_LABEL[c.auth_type]}{c.has_credential ? " · credencial guardada" : ""}</div></td>
              <td className="pr-3 text-xs">{c.allowed_methods.join(", ")}<div className="text-slate-400">{c.allowed_paths.join(", ")}</div></td>
              <td className="space-x-3 whitespace-nowrap">
                <button onClick={() => test(c)} className="text-slate-700 underline">Probar</button>
                <button onClick={() => act(() => api(`/api/v1/integrations/${c.name}`, "admin", { method: "PATCH", body: JSON.stringify({ enabled: !c.enabled }) }), c.enabled ? "Desactivado" : "Activado")} className="text-slate-700 underline">{c.enabled ? "Desactivar" : "Activar"}</button>
                <button onClick={() => confirm(`¿Eliminar el conector ${c.name}?`) && act(() => api(`/api/v1/integrations/${c.name}`, "admin", { method: "DELETE" }), "Conector eliminado")} className="text-red-600">Eliminar</button>
              </td>
            </tr>
          ))}
          {items.length === 0 && <tr><td colSpan={4} className="py-3 text-slate-400">Sin conectores todavía.</td></tr>}
        </tbody>
      </table>
    </main>
  );
}
