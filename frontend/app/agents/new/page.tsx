"use client";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Agent, api, Catalog, PhoneNumber, SimResult } from "@/lib/api";

const STEPS = ["Información", "Voz", "Instrucciones", "Conocimiento", "Herramientas", "Integraciones", "Telefonía", "Pruebas", "Publicación"];
const TOOL_HELP: Record<string, string> = {
  check_inventory: "Consultar existencias", check_reservation: "Consultar una reserva", create_reservation: "Crear reservas (pide confirmación)",
  modify_reservation: "Modificar reservas (pide confirmación)", cancel_reservation: "Cancelar reservas (pide confirmación)",
  create_support_ticket: "Abrir tickets de soporte", search_knowledge_base: "Buscar en la base de conocimiento", transfer_to_human: "Transferir a una persona",
  end_call: "Finalizar la llamada", lookup_customer: "Reconocer a quien llama", register_customer: "Registrar al cliente (pide confirmación)",
  check_order: "Consultar pedidos de quien llama", send_confirmation: "Enviar SMS de confirmación a quien llama",
};

export default function NewAgentWizard() {
  const [step, setStep] = useState(0);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [allTools, setAllTools] = useState<string[]>([]);
  const [phones, setPhones] = useState<PhoneNumber[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [business, setBusiness] = useState("");
  const [goal, setGoal] = useState("");
  const [language, setLanguage] = useState("es");
  const [voice, setVoice] = useState("");
  const [tone, setTone] = useState("amable y breve");
  const [tasks, setTasks] = useState("");
  const [restrictions, setRestrictions] = useState("");
  const [tools, setTools] = useState<string[]>(["transfer_to_human", "end_call"]);
  const [phoneId, setPhoneId] = useState("");

  const [agentId, setAgentId] = useState<string | null>(null);
  const [turns, setTurns] = useState("Hola, quiero hacer una consulta.");
  const [sim, setSim] = useState<SimResult | null>(null);
  const [simNote, setSimNote] = useState<string | null>(null);
  const [published, setPublished] = useState(false);

  useEffect(() => {
    Promise.all([
      api<Catalog>("/api/v1/catalog", "admin"),
      api<{ available_tools: string[] }>("/api/v1/agents", "admin"),
      api<{ items: PhoneNumber[] }>("/api/v1/phone-numbers", "admin"),
    ]).then(([c, a, p]) => { setCatalog(c); setAllTools(a.available_tools); setPhones(p.items); }).catch((e) => setErr(e.message));
  }, []);

  const instructions = useMemo(() => [
    business && `Negocio: ${business.trim()}`,
    goal && `Objetivo: ${goal.trim()}`,
    `Personalidad y tono: ${tone.trim() || "amable y breve"}.`,
    tasks && `Tareas que puedes hacer:\n${tasks.trim()}`,
    restrictions && `Restricciones (nunca debes):\n${restrictions.trim()}`,
  ].filter(Boolean).join("\n\n"), [business, goal, tone, tasks, restrictions]);

  const problem = (s: number): string | null => {
    if (s === 0 && name.trim().length < 2) return "Escribe un nombre de al menos 2 caracteres";
    if (s === 0 && goal.trim().length < 5) return "Describe el objetivo del agente";
    if (s === 2 && instructions.length < 10) return "Añade tareas o restricciones";
    if (s === 2 && instructions.length > 4000) return `Las instrucciones miden ${instructions.length} caracteres; el máximo es 4000`;
    if (s === 4 && tools.length === 0) return "Elige al menos una herramienta";
    return null;
  };

  const next = () => {
    const p = problem(step);
    if (p) return setErr(p);
    setErr(null);
    setStep((s) => Math.min(s + 1, STEPS.length - 1));
  };

  const body = () => ({ name: name.trim(), instructions, tools, voice: voice || null, language });

  const ensureDraft = async (): Promise<string> => {
    if (agentId) {
      await api(`/api/v1/agents/${agentId}`, "admin", { method: "PATCH", body: JSON.stringify(body()) }); // nueva versión con los cambios
      return agentId;
    }
    const a = await api<Agent>("/api/v1/agents", "admin", { method: "POST", body: JSON.stringify(body()) });
    setAgentId(a.id);
    return a.id;
  };

  const runTest = async () => {
    setBusy(true); setErr(null); setSimNote(null); setSim(null);
    try {
      const id = await ensureDraft();
      const list = turns.split("\n").map((t) => t.trim()).filter(Boolean);
      setSim(await api<SimResult>(`/api/v1/agents/${id}/simulate`, "admin", { method: "POST", body: JSON.stringify({ turns: list }) }));
    } catch (e) {
      const m = (e as Error).message;
      if (m.includes("Simulador no configurado")) setSimNote("El simulador no está activado en este servidor (falta ANTHROPIC_API_KEY). El agente se guardó como borrador; puedes publicarlo sin probar, pero no se recomienda.");
      else setErr(m);
    } finally { setBusy(false); }
  };

  const publish = async () => {
    setBusy(true); setErr(null);
    try {
      const id = await ensureDraft();
      await api(`/api/v1/agents/${id}/publish`, "admin", { method: "POST" });
      if (phoneId) await api(`/api/v1/phone-numbers/${phoneId}`, "admin", { method: "PATCH", body: JSON.stringify({ agent_id: id }) });
      setPublished(true);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };

  const phone = phones.find((p) => p.id === phoneId);

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Crear un agente</h1>
        <Link href="/agents" className="text-sm text-slate-500 underline">Volver a agentes</Link>
      </div>
      <ol aria-label="Pasos" className="flex flex-wrap gap-2 text-xs">
        {STEPS.map((s, i) => (
          <li key={s} aria-current={i === step ? "step" : undefined}
            className={`rounded px-2 py-1 ${i === step ? "bg-slate-900 text-white" : i < step ? "bg-emerald-100 text-emerald-800" : "bg-slate-100 text-slate-500"}`}>{i + 1}. {s}</li>
        ))}
      </ol>
      {err && <p role="alert" className="rounded bg-red-50 p-3 text-sm text-red-700">{err}</p>}

      <section className="space-y-4 rounded border p-4">
        {step === 0 && (<>
          <label className="block text-sm">Nombre del agente<input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} className="mt-1 w-full rounded border p-2" /></label>
          <label className="block text-sm">Sobre tu negocio<textarea value={business} onChange={(e) => setBusiness(e.target.value)} placeholder="Clínica dental en Providencia, atendemos de lunes a sábado." className="mt-1 h-20 w-full rounded border p-2" /></label>
          <label className="block text-sm">Objetivo del agente<textarea value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="Agendar y reprogramar citas y responder preguntas frecuentes." className="mt-1 h-20 w-full rounded border p-2" /></label>
        </>)}
        {step === 1 && (<>
          <label className="block text-sm">Idioma
            <select value={language} onChange={(e) => setLanguage(e.target.value)} className="mt-1 w-full rounded border p-2">
              {(catalog?.languages ?? [{ code: "es", name: "español" }]).map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
            </select></label>
          <label className="block text-sm">Voz
            <select value={voice} onChange={(e) => setVoice(e.target.value)} className="mt-1 w-full rounded border p-2">
              <option value="">Predeterminada</option>
              {(catalog?.voices ?? []).map((v) => <option key={v}>{v}</option>)}
            </select></label>
          <label className="block text-sm">Personalidad y tono<input value={tone} onChange={(e) => setTone(e.target.value)} maxLength={120} className="mt-1 w-full rounded border p-2" /></label>
          <p className="text-xs text-slate-500">La velocidad de habla todavía no es configurable por agente.</p>
        </>)}
        {step === 2 && (<>
          <label className="block text-sm">Tareas que puede hacer (una por línea)<textarea value={tasks} onChange={(e) => setTasks(e.target.value)} className="mt-1 h-28 w-full rounded border p-2" /></label>
          <label className="block text-sm">Restricciones: lo que nunca debe hacer<textarea value={restrictions} onChange={(e) => setRestrictions(e.target.value)} placeholder="Dar diagnósticos. Prometer descuentos." className="mt-1 h-24 w-full rounded border p-2" /></label>
          <details className="text-xs text-slate-500"><summary>Instrucciones resultantes ({instructions.length}/4000)</summary><pre className="mt-2 whitespace-pre-wrap rounded bg-slate-50 p-2">{instructions || "—"}</pre></details>
          <p className="text-xs text-slate-500">Además de esto, la plataforma aplica sus propias reglas (no inventar datos, confirmar antes de cambiar algo, derivar a una persona) que tus instrucciones no pueden desactivar.</p>
        </>)}
        {step === 3 && <p className="text-sm text-slate-600">La base de conocimiento con documentos (PDF, DOCX, CSV) <b>aún no está disponible</b>. Hoy el agente puede buscar en la tabla de conocimiento de la organización con la herramienta «Buscar en la base de conocimiento».</p>}
        {step === 4 && (
          <fieldset><legend className="text-sm">Herramientas permitidas (mínimo privilegio: da solo las necesarias)</legend>
            <div className="mt-2 grid gap-2 md:grid-cols-2">
              {allTools.map((t) => (
                <label key={t} className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={tools.includes(t)}
                  onChange={(e) => setTools((s) => (e.target.checked ? [...s, t] : s.filter((x) => x !== t)))} />
                  <span><b>{t}</b><br /><span className="text-xs text-slate-500">{TOOL_HELP[t] ?? ""}</span></span></label>
              ))}
            </div></fieldset>
        )}
        {step === 5 && <p className="text-sm text-slate-600">Las integraciones con calendarios y CRM (Google Calendar, Outlook, HubSpot, Salesforce) <b>aún no existen</b>. Puedes conectar tus sistemas con la API, claves y webhooks desde <Link className="underline" href="/integrations">Integraciones</Link>.</p>}
        {step === 6 && (<>
          <label className="block text-sm">Número que atenderá este agente (opcional)
            <select value={phoneId} onChange={(e) => setPhoneId(e.target.value)} className="mt-1 w-full rounded border p-2">
              <option value="">Ninguno por ahora</option>
              {phones.map((p) => <option key={p.id} value={p.id}>{p.e164}{p.agent_id ? " (ya tiene un agente; se reemplazará)" : ""}</option>)}
            </select></label>
          {phones.length === 0 && <p className="text-xs text-slate-500">Aún no hay números. Añádelos en <Link className="underline" href="/phones">Números</Link>.</p>}
          <p className="text-xs text-slate-500">El horario de atención aún no es configurable por agente.</p>
        </>)}
        {step === 7 && (<>
          <label className="block text-sm">Mensajes de prueba del cliente (uno por línea, hasta 10)<textarea value={turns} onChange={(e) => setTurns(e.target.value)} className="mt-1 h-24 w-full rounded border p-2" /></label>
          <button disabled={busy} onClick={runTest} className="rounded bg-indigo-600 px-4 py-2 text-sm text-white disabled:opacity-50">{busy ? "Probando…" : "Guardar borrador y probar"}</button>
          {simNote && <p role="status" className="rounded bg-amber-50 p-3 text-sm text-amber-800">{simNote}</p>}
          {sim && (<div className="space-y-2 text-sm">
            <p className="rounded bg-slate-50 p-2 text-xs text-slate-600">{sim.note}</p>
            {sim.transcript.map((t, i) => <p key={i} className={t.role === "user" ? "text-slate-700" : t.role === "agent" ? "text-indigo-700" : "text-slate-400"}><b>{t.role === "user" ? "Cliente" : t.role === "agent" ? "Agente" : "Sistema"}:</b> {t.text}</p>)}
            {sim.tools.length > 0 && <p className="text-xs text-slate-500">Herramientas: {sim.tools.map((t) => `${t.tool} (${t.ok ? "ok" : t.error})`).join(", ")}</p>}
            {sim.transferred && <p className="text-xs text-slate-500">El agente pidió transferir a una persona.</p>}
          </div>)}
        </>)}
        {step === 8 && !published && (<>
          <p className="text-sm">Resumen: <b>{name}</b> · {language}{voice ? ` · voz ${voice}` : ""} · {tools.length} herramientas · {phone ? `número ${phone.e164}` : "sin número"}.</p>
          <p className="text-sm text-slate-600">Al publicar, el agente podrá atender llamadas reales {phone ? `en ${phone.e164}` : "en los números que le asignes"}. Puedes volver a un borrador archivándolo o revertir a una versión anterior.</p>
          <button disabled={busy} onClick={publish} className="rounded bg-emerald-600 px-4 py-2 text-sm text-white disabled:opacity-50">{busy ? "Publicando…" : "Publicar agente"}</button>
        </>)}
        {step === 8 && published && <p role="status" className="rounded bg-emerald-50 p-3 text-sm text-emerald-700">Agente publicado. <Link href="/agents" className="underline">Ver agentes</Link></p>}
      </section>

      {!published && (
        <div className="flex justify-between">
          <button disabled={step === 0} onClick={() => { setErr(null); setStep((s) => s - 1); }} className="rounded border px-4 py-2 text-sm disabled:opacity-40">Atrás</button>
          {step < STEPS.length - 1 && <button onClick={next} className="rounded bg-slate-900 px-4 py-2 text-sm text-white">Siguiente</button>}
        </div>
      )}
    </main>
  );
}
