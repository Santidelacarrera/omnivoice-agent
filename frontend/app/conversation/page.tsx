"use client";
import { useRef, useState } from "react";
import { getToken } from "@/lib/api";
import { AgentState, VoiceClient } from "@/lib/voiceClient";

const LABEL: Record<AgentState, string> = {
  idle: "Inactivo", connecting: "Conectando…", listening: "Escuchando", processing: "Procesando",
  responding: "Respondiendo", interrupted: "Interrumpido", error: "Error de conexión",
};

export default function Conversation() {
  const [state, setState] = useState<AgentState>("idle");
  const [consent, setConsent] = useState(false);
  const [muted, setMuted] = useState(false);
  const [level, setLevel] = useState(0);
  const [lines, setLines] = useState<{ who: "user" | "agent"; text: string }[]>([]);
  const [metrics, setMetrics] = useState<{ firstAudioMs?: number; bargeInMs?: number }>({});
  const [tool, setTool] = useState<string | null>(null);
  const client = useRef<VoiceClient | null>(null);
  const active = state !== "idle" && state !== "error";

  async function toggle() {
    if (active) { await client.current?.stop(); return; }
    // Demo: token de desarrollo. En producción, el token proviene de tu proveedor de identidad.
    let token: string;
    try { token = await getToken("customer"); } catch { setState("error"); return; }
    setLines([]); setMetrics({});
    client.current = new VoiceClient(token, {
      onState: setState,
      onTranscript: (who, text) => setLines((l) => [...l, { who, text }]),
      onMetrics: (m) => setMetrics((p) => ({ ...p, ...m })),
      onTool: (n, phase) => setTool(phase === "start" ? n : null),
      onLevel: setLevel,
    });
    try { await client.current.start(); } catch { setState("error"); }
  }

  return (
    <main className="mx-auto max-w-2xl p-6 space-y-6">
      <h1 className="text-2xl font-semibold">OmniVoice Agent</h1>
      <p className="text-sm text-slate-500" role="note">
        Esta conversación usa tu micrófono y puede transcribirse y registrarse según la política de privacidad.
      </p>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        Acepto el uso del micrófono y el tratamiento de la conversación.
      </label>

      <div className="flex items-center gap-4">
        <button onClick={toggle} disabled={!consent && !active}
          className="rounded-full bg-indigo-600 px-6 py-3 text-white disabled:opacity-40" aria-live="polite">
          {active ? "Finalizar" : "Iniciar conversación"}
        </button>
        <button onClick={() => { const m = !muted; setMuted(m); if (client.current) client.current.muted = m; }}
          disabled={!active} className="rounded-full border px-4 py-3 disabled:opacity-40">
          {muted ? "Activar micrófono" : "Silenciar"}
        </button>
        <span className="text-sm font-medium" role="status">{LABEL[state]}{tool ? ` · ${tool}` : ""}</span>
      </div>

      <div className="h-3 w-full overflow-hidden rounded bg-slate-200" aria-hidden>
        <div className="h-full bg-emerald-500 transition-[width] duration-75" style={{ width: `${Math.min(100, level * 400)}%` }} />
      </div>

      <section className="space-y-2 rounded border p-4 min-h-40" aria-label="Transcripción en vivo">
        {lines.length === 0 && <p className="text-slate-400 text-sm">La transcripción aparecerá aquí.</p>}
        {lines.map((l, i) => (
          <p key={i} className={l.who === "user" ? "text-slate-700" : "text-indigo-700"}>
            <b>{l.who === "user" ? "Tú" : "Agente"}:</b> {l.text}
          </p>
        ))}
      </section>

      <dl className="grid grid-cols-2 gap-2 text-sm">
        <div className="rounded bg-slate-100 p-3"><dt className="text-slate-500">Primer audio</dt><dd>{metrics.firstAudioMs ?? "—"} ms</dd></div>
        <div className="rounded bg-slate-100 p-3"><dt className="text-slate-500">Silencio tras interrupción</dt><dd>{metrics.bargeInMs ?? "—"} ms</dd></div>
      </dl>
    </main>
  );
}
