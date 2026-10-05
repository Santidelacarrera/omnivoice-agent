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
