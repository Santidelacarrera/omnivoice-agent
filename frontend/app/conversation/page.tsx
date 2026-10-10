"use client";
import { useEffect, useRef, useState } from "react";
import { api, Catalog, getToken } from "@/lib/api";
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
  const [metrics, setMetrics] = useState<{ firstAudioMs?: number; bargeInMs?: number; sttMs?: number; llmMs?: number; ttsMs?: number }>({});
  const [tool, setTool] = useState<string | null>(null);
  const [toolResult, setToolResult] = useState<{ name: string; ok?: boolean; data?: unknown } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [voice, setVoice] = useState("");
  const [language, setLanguage] = useState("");
  const [recordConsent, setRecordConsent] = useState(false);
  const [recording, setRecording] = useState(false);
  const client = useRef<VoiceClient | null>(null);
  const active = state !== "idle" && state !== "error";

  useEffect(() => {
    // Si el catálogo no responde, la pantalla sigue funcionando con los valores del agente.
    api<Catalog>("/api/v1/catalog", "customer").then(setCatalog).catch(() => setCatalog(null));
  }, []);

  async function toggle() {
    if (active) { await client.current?.stop(); return; }
    // Demo: token de desarrollo. En producción, el token proviene de tu proveedor de identidad.
    let token: string;
    try { token = await getToken("customer"); } catch { setState("error"); return; }
    setLines([]); setMetrics({}); setError(null); setToolResult(null); setNotice(null); setRecording(false);
    client.current = new VoiceClient(token, {
      onState: setState,
      onTranscript: (who, text) => setLines((l) => [...l, { who, text }]),
      onMetrics: (m) => setMetrics((p) => ({ ...p, ...m })),
      onTool: (n, phase, ok, data) => {
        setTool(phase === "start" ? n : null);
        if (phase === "end") setToolResult({ name: n, ok, data });
      },
      onNotice: setNotice,
      onLevel: setLevel,
      onError: setError,
      onRecording: setRecording,
    }, {
      voice: voice || undefined,
      language: language || undefined,
      recordingConsent: recordConsent && !!catalog?.recording.available,
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

      {catalog && (
        <div className="grid grid-cols-2 gap-4">
          <label className="text-sm space-y-1">
            <span className="block text-slate-500">Voz</span>
            <select value={voice} onChange={(e) => setVoice(e.target.value)} disabled={active}
              className="w-full rounded border p-2 disabled:opacity-40">
              <option value="">Predeterminada del agente</option>
              {catalog.voices.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </label>
          <label className="text-sm space-y-1">
            <span className="block text-slate-500">Idioma</span>
            <select value={language} onChange={(e) => setLanguage(e.target.value)} disabled={active}
              className="w-full rounded border p-2 disabled:opacity-40">
              <option value="">Predeterminado del agente</option>
              {catalog.languages.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
            </select>
          </label>
        </div>
      )}
      {catalog?.recording.available && (
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={recordConsent} disabled={active} className="mt-1"
            onChange={(e) => setRecordConsent(e.target.checked)} />
          <span>
            Autorizo además que se <b>grabe el audio</b> de esta llamada para calidad y auditoría. Es opcional,
            puedes retirarlo en cualquier momento y se borrará según la política de retención.
          </span>
        </label>
      )}

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

      {recording && (
        <p role="status" className="flex items-center gap-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <span className="h-2 w-2 animate-pulse rounded-full bg-red-600" aria-hidden /> Grabando esta llamada.
          <button onClick={() => client.current?.revokeRecording()} className="ml-auto underline">Dejar de grabar y borrar</button>
        </p>
      )}

      {error && (
        <p role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800">
          {error === "provider_unavailable" ? "El proveedor de voz no está disponible. Revisa los registros del backend." : error}
        </p>
      )}

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
        {metrics.sttMs !== undefined && (
          <div className="col-span-2 rounded bg-slate-100 p-3">
            <dt className="text-slate-500">Última respuesta por etapas</dt>
            <dd>STT {metrics.sttMs} ms · LLM {metrics.llmMs ?? "—"} ms · TTS {metrics.ttsMs ?? "—"} ms</dd>
          </div>
        )}
      </dl>

      {notice && <p role="status" className="rounded bg-amber-100 p-3 text-sm text-amber-900">{notice}</p>}
      {toolResult && (
        <section aria-label="Última herramienta" className="rounded border p-3 text-sm">
          <b>{toolResult.name}</b> {toolResult.ok ? "✓" : "✗"}
          <pre className="mt-1 overflow-x-auto text-xs text-slate-600">{JSON.stringify(toolResult.data ?? null, null, 2)}</pre>
        </section>
      )}
    </main>
  );
}
