export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const DEMO_ORG = "00000000-0000-0000-0000-000000000001";

export type Role = "admin" | "operator" | "customer";

// Tokens solo en memoria (nunca en localStorage). En producción el JWT lo emite tu proveedor de identidad;
// este flujo usa /auth/dev-token, que el backend desactiva con ENVIRONMENT=production.
const cache = new Map<Role, { token: string; exp: number }>();

export async function getToken(role: Role): Promise<string> {
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

export async function api<T>(path: string, role: Role, init: RequestInit = {}): Promise<T> {
  const token = await getToken(role);
  const r = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Error ${r.status}`);
  }
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
