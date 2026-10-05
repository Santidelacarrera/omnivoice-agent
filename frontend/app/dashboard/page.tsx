"use client";
import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function parseProm(text: string, name: string) {
  const re = new RegExp(`^${name}(?:\\{[^}]*\\})? ([0-9.e+-]+)$`, "gm");
  let total = 0, m;
  while ((m = re.exec(text))) total += parseFloat(m[1]);
  return total;
}

export default function Dashboard() {
  const [m, setM] = useState<Record<string, number>>({});
  const [audit, setAudit] = useState<any[]>([]);

  useEffect(() => {
    const tick = async () => {
      const t = await fetch(`${API}/metrics`).then((r) => r.text());
      const sum = parseProm(t, "omnivoice_first_audio_seconds_sum");
      const cnt = parseProm(t, "omnivoice_first_audio_seconds_count");
      setM({
        sesiones: parseProm(t, "omnivoice_active_sessions"),
        ttfbMedio: cnt ? Math.round((sum / cnt) * 1000) : 0,
        errores: parseProm(t, "omnivoice_errors_total"),
        paquetesPerdidos: parseProm(t, "omnivoice_packets_lost_total"),
      });
      const tok = (await fetch(`${API}/api/v1/auth/dev-token`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: "admin", org_id: "o1", role: "admin" }),
      }).then((r) => r.json())).token;
      const a = await fetch(`${API}/api/v1/audit-logs`, { headers: { Authorization: `Bearer ${tok}` } }).then((r) => r.json());
      setAudit(a.items.slice(-20).reverse());
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => clearInterval(id);
  }, []);

  const cards: [string, string | number][] = [
    ["Sesiones activas", m.sesiones ?? 0],
    ["Latencia media (1er audio)", `${m.ttfbMedio ?? 0} ms`],
    ["Errores", m.errores ?? 0],
    ["Paquetes perdidos", m.paquetesPerdidos ?? 0],
  ];
  return (
    <main className="mx-auto max-w-4xl p-6 space-y-6">
      <h1 className="text-2xl font-semibold">Panel de administración</h1>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {cards.map(([k, v]) => (
          <div key={k} className="rounded border p-4"><div className="text-xs text-slate-500">{k}</div><div className="text-2xl">{v}</div></div>
        ))}
      </div>
      <h2 className="text-lg font-medium">Auditoría reciente</h2>
      <table className="w-full text-sm">
        <thead><tr className="text-left text-slate-500"><th>Hora</th><th>Evento</th><th>Detalle</th></tr></thead>
        <tbody>
          {audit.map((e, i) => (
            <tr key={i} className="border-t">
              <td>{new Date(e.ts * 1000).toLocaleTimeString()}</td><td>{e.event}</td>
              <td className="truncate max-w-xs">{JSON.stringify({ tool: e.tool, ok: e.ok })}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
