export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const DEMO_ORG = "00000000-0000-0000-0000-000000000001";

export type Role = "owner" | "admin" | "manager" | "operator" | "analyst" | "viewer" | "customer";

// Tokens solo en sessionStorage (se borra al cerrar la pestaña; nunca en localStorage)
const KEY = "omnivoice.token";

export function decodeToken(token: string): { sub: string; org: string; role: Role; exp: number } | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    if (typeof payload.exp !== "number" || !payload.role || !payload.org) return null;
    return { sub: String(payload.sub), org: String(payload.org), role: payload.role, exp: payload.exp };
  } catch { return null; }
}

export function getStoredToken(): string | null {
  try {
    const t = sessionStorage.getItem(KEY);
    const info = t ? decodeToken(t) : null;
    if (t && info && info.exp * 1000 > Date.now() + 10_000) return t;
    if (t) sessionStorage.removeItem(KEY);
  } catch { /* sessionStorage no disponible */ }
  return null;
}

export function storeToken(token: string): { sub: string; org: string; role: Role; exp: number } {
  const info = decodeToken(token.trim());
  if (!info) throw new Error("El token no tiene un formato válido");
  if (info.exp * 1000 <= Date.now()) throw new Error("El token ya expiró");
  sessionStorage.setItem(KEY, token.trim());
  return info;
}

export function clearToken(): void {
  try { sessionStorage.removeItem(KEY); } catch { /* nada */ }
}

const cache = new Map<Role, { token: string; exp: number }>();

export async function getToken(role: Role): Promise<string> {
  const stored = getStoredToken();
  if (stored) return stored;
  const hit = cache.get(role);
  if (hit && hit.exp > Date.now() + 30_000) return hit.token;
  const r = await fetch(`${API}/api/v1/auth/dev-token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: `demo-${role}`, org_id: DEMO_ORG, role }),
  });
  if (!r.ok) throw new Error(`No se pudo obtener el token (${r.status})`);
  const { token } = await r.json();
  cache.set(role, { token, exp: Date.now() + 55 * 60_000 });
  return token;
}

export async function registerUser(email: string, orgName: string, password: string): Promise<{ org_id: string; user_id: string; token: string }> {
  const r = await fetch(`${API}/api/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, org_name: orgName, password }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Error ${r.status}`);
  }
  return r.json();
}

export async function loginUser(email: string, password: string): Promise<{ token: string }> {
  const r = await fetch(`${API}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Error ${r.status}`);
  }
  return r.json();
}

export async function getCurrentUser(): Promise<{ user_id: string; org_id: string; role: string }> {
  const r = await fetch(`${API}/api/v1/users/me`, {
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${await getToken("customer")}` },
  });
  if (!r.ok) throw new Error("No autenticado");
  return r.json();
}

export async function getRoles(): Promise<Record<string, string[]>> {
  const r = await fetch(`${API}/api/v1/roles`, {
    headers: { "Content-Type": "application/json" },
  });
  if (!r.ok) throw new Error("Error al obtener roles");
  const data = await r.json();
  const roles: Record<string, string[]> = {};
  for (const entry of data.roles) {
    const [role, perms] = Object.entries(entry)[0];
    if (Array.isArray(perms)) roles[role] = perms;
  }
  return roles;
}

export async function getPlan(): Promise<{ plan: string; limits: Record<string, number>; usage: Record<string, number> }> {
  const r = await fetch(`${API}/api/v1/billing/plan`, {
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${await getToken("operator")}` },
  });
  if (!r.ok) throw new Error("Error al obtener plan");
  return r.json();
}

export async function getBillingHistory(): Promise<{ items: { period: string; amount_usd: number; status: string }[]; total_paid_usd: number }> {
  const r = await fetch(`${API}/api/v1/billing/history`, {
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${await getToken("operator")}` },
  });
  if (!r.ok) throw new Error("Error al obtener historial");
  return r.json();
}

export async function getKnowledgeList(): Promise<{ items: { id: string; title: string; type: string; size_kb: number; created_at: string }[]; total_size_kb: number; max_size_kb: number }> {
  const r = await fetch(`${API}/api/v1/knowledge`, {
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${await getToken("operator")}` },
  });
  if (!r.ok) throw new Error("Error al obtener conocimiento");
  return r.json();
}

export async function searchKnowledge(query: string): Promise<{ results: { title: string; snippet: string; source_id: string; ordinal: number }[]; count: number }> {
  const r = await fetch(`${API}/api/v1/knowledge/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${await getToken("operator")}` },
    body: JSON.stringify({ q: query }),
  });
  if (!r.ok) throw new Error("Error al buscar conocimiento");
  return r.json();
}

export async function api<T>(path: string, role: Role, init: RequestInit = {}): Promise<T> {
  const token = await getToken(role);
  const r = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    if (r.status === 404 && body.detail === "Not Found") throw new Error("Esta función no está habilitada en el servidor (p. ej. la telefonía no está configurada).");
    throw new Error(typeof body.detail === "string" ? body.detail : `Error ${r.status}`);
  }
  if (r.status === 204) return undefined as T; // DELETE correcto: sin cuerpo
  return r.json();
}

export interface Percentiles { count: number; p50: number | null; p95: number | null; p99: number | null }
export interface MetricsResponse {
  active_sessions: number;
  first_audio_ms: Percentiles;
  barge_in_ms: Percentiles;
  usage: { audio_seconds: number; estimated_cost_usd: number };
}
export interface ConversationRow {
  id: string; created_at: number; final_state: string | null;
  first_audio_ms: number | null; interruptions: number; lost_packets: number;
}
export interface ConversationDetail extends ConversationRow {
  transcript: { speaker: "user" | "agent"; text: string; ts: number }[];
  tools: { tool_name?: string; tool?: string; ok: boolean | null; duration_ms: number | null }[];
}
export interface Agent { id: string; name: string; instructions: string; tools: string[]; voice: string | null; language: string }

// ---- Etapas 2–3 y 9–10: llamadas, números y analítica ----
export interface CallRow {
  id: string; sid: string; direction: "inbound" | "outbound"; status: string; our_number: string | null;
  created_at: number; answered_at: number | null; ended_at: number | null;
}
export interface CallDetail extends CallRow {
  events: { status: string; event_key: string; applied: boolean; detail: Record<string, unknown>; ts: number }[];
}
export interface PhoneNumber { id: string; e164: string; agent_id: string | null; transfer_number: string | null }
export interface Analytics {
  period_days: number;
  calls: { total: number; inbound: number; outbound: number; answered: number; missed: number; transferred: number; avg_duration_s: number | null };
  rates: { answer_rate: number | null; missed_rate: number | null; transfer_rate: number | null };
  by_day: { date: string; calls: number; answered: number; missed: number; transferred: number }[];
}

export interface OutboundJob {
  id: string; status: string; attempts: number; max_attempts: number; next_attempt_at: string; timezone: string; from_number: string;
  consent_basis: string; to_masked: string | null; number_ref: string; last_result: string | null;
}
export interface DncEntry { id: string; number_ref: string; reason: string | null; created_at: number }
export interface ApiKey { id: string; name: string; prefix: string; role: string; last_used_at: number | null; expires_at: number | null; revoked: boolean }
export interface Webhook { id: string; url: string; events: string[]; description: string | null; active: boolean }
export interface Delivery { id: number; event_type: string; status: string; attempts: number; last_status: number | null; last_error: string | null }
export interface AuditEntry { ts: number; action: string; actor: string | null; detail: Record<string, unknown> }
export interface SimResult {
  sandbox: boolean; note: string; transcript: { role: "user" | "agent" | "system"; text: string }[];
  tools: { tool: string; ok: boolean; error: string | null }[]; transferred: boolean;
}

export const CONSENT_LABELS: Record<string, string> = {
  explicit_opt_in: "Consentimiento explícito (exige indicar cómo y cuándo)",
  existing_customer: "Cliente existente",
  transactional: "Comunicación transaccional",
};

export interface PlanInfo {
  plan: string; name: string | null; enforced: boolean;
  limits: Record<string, number | null>; percent: Record<string, number | null>;
  usage: { minutes_month: number; agents: number; phone_numbers: number; outbound_today: number; kb_sources: number; estimated_cost_usd_month: number };
}

export const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)} %`);
export const secs = (v: number | null) => (v === null ? "—" : v >= 60 ? `${Math.floor(v / 60)} min ${Math.round(v % 60)} s` : `${Math.round(v)} s`);

export const STATUS_STYLE: Record<string, string> = {
  CONNECTED: "bg-green-100 text-green-800", ANSWERED: "bg-green-100 text-green-800", COMPLETED: "bg-slate-100 text-slate-700",
  TRANSFERRED: "bg-indigo-100 text-indigo-800", FAILED: "bg-red-100 text-red-800", BUSY: "bg-amber-100 text-amber-800",
  NO_ANSWER: "bg-amber-100 text-amber-800", CANCELLED: "bg-slate-100 text-slate-500",
};

export interface Catalog {
  voices: string[];
  languages: { code: string; name: string }[];
  recording: { available: boolean };
}
