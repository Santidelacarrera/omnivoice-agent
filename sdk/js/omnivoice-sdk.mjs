/**
 * SDK mínimo de OmniVoice Agent para JavaScript (Node >= 18, navegadores y Deno). Sin dependencias.
 *
 *   import { OmniVoice } from "./omnivoice-sdk.mjs";
 *   const ov = new OmniVoice("https://api.tu-dominio.com", { apiKey: "ovk_..." });
 *   const agent = await ov.agents.create({ name: "Recepción", instructions: "Atiende llamadas con amabilidad." });
 *   await ov.agents.publish(agent.id);
 *   await ov.calls.create({ to: "+56987654321", consentBasis: "existing_customer" });
 *
 * Una clave de API no puede gestionar claves, webhooks ni equipo (la API lo prohíbe): usa un JWT de una persona.
 * No pongas una clave de API en código que se ejecute en el navegador de tus clientes.
 * `verifyWebhook` usa node:crypto, así que solo está disponible en Node/Deno/Bun.
 */
import { createHmac, timingSafeEqual } from "node:crypto";

export class OmniVoiceError extends Error {
  constructor(status, message) {
    super(`${status}: ${message}`);
    this.name = "OmniVoiceError";
    this.status = status;
    this.detail = message;
  }
}

/** Valida `X-OmniVoice-Signature` sobre el cuerpo **crudo** (string). Falla si la firma no coincide o `t` supera la tolerancia. */
export function verifyWebhook(secret, signatureHeader, body, { toleranceS = 300, now = Date.now() / 1000 } = {}) {
  const parts = {};
  for (const p of String(signatureHeader ?? "").split(",")) {
    const i = p.indexOf("=");
    if (i > 0) parts[p.slice(0, i).trim()] = p.slice(i + 1).trim();
  }
  const t = Number(parts.t);
  if (!Number.isInteger(t) || !parts.v1 || Math.abs(now - t) > toleranceS) return false;
  const expected = createHmac("sha256", secret).update(`${t}.${body}`).digest("hex");
  const a = Buffer.from(expected), b = Buffer.from(parts.v1);
  return a.length === b.length && timingSafeEqual(a, b);
}

const clean = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null));

export class OmniVoice {
  constructor(baseUrl, { apiKey, token, timeoutMs = 15000, fetch: f = globalThis.fetch } = {}) {
    const credential = apiKey ?? token;
    if (!credential) throw new TypeError("Falta apiKey (o token JWT)");
    this.base = baseUrl.replace(/\/+$/, "");
    this._auth = `Bearer ${credential}`;
    this._timeout = timeoutMs;
    this._fetch = f;
    const c = this;
    this.agents = {
      list: async () => (await c._get("/agents")).items,
      get: (id) => c._get(`/agents/${id}`),
      create: ({ name, instructions, tools = [], voice = null, language = "es" }) => c._send("POST", "/agents", { name, instructions, tools, voice, language }),
      update: (id, changes) => c._send("PATCH", `/agents/${id}`, changes),
      versions: async (id) => (await c._get(`/agents/${id}/versions`)).items,
      rollback: (id, version) => c._send("POST", `/agents/${id}/rollback`, { version }),
      publish: (id) => c._send("POST", `/agents/${id}/publish`, {}),
      archive: (id) => c._send("POST", `/agents/${id}/archive`, {}),
      simulate: (id, turns) => c._send("POST", `/agents/${id}/simulate`, { turns }),
    };
    this.calls = {
      list: async (limit = 50) => (await c._get("/calls", { limit })).items,
      get: (id) => c._get(`/calls/${id}`),
      /** `consentBasis`: explicit_opt_in (exige consentRef) | existing_customer | transactional. */
      create: ({ to, consentBasis, consentRef, scheduledAt, timezone, maxAttempts, fromNumber, idempotencyKey }) =>
        c._send("POST", "/calls", clean({ to, consent_basis: consentBasis, consent_ref: consentRef, scheduled_at: scheduledAt, timezone,
          max_attempts: maxAttempts, from_number: fromNumber, idempotency_key: idempotencyKey })),
      transfer: (id, reason = "") => c._send("POST", `/calls/${id}/transfer`, { reason }),
      end: (id) => c._send("POST", `/calls/${id}/end`, {}),
    };
    this.outbound = {
      list: async () => (await c._get("/outbound-calls")).items,
      get: (id) => c._get(`/outbound-calls/${id}`),
      cancel: (id) => c._send("POST", `/outbound-calls/${id}/cancel`, {}),
      doNotCall: (number, reason) => c._send("POST", "/do-not-call", clean({ number, reason })),
    };
    this.webhooks = {
      list: () => c._get("/webhooks"),
      /** Devuelve el secreto de firma **una sola vez**. */
      create: ({ url, events, description }) => c._send("POST", "/webhooks", clean({ url, events, description })),
      delete: (id) => c._send("DELETE", `/webhooks/${id}`),
      deliveries: async (id) => (await c._get(`/webhooks/${id}/deliveries`)).items,
    };
    this.team = {
      list: async () => (await c._get("/team")).items,
      set: (email, role) => c._send("PUT", `/team/${encodeURIComponent(email)}`, { role }),
      remove: (email) => c._send("DELETE", `/team/${encodeURIComponent(email)}`),
    };
  }

  usage(days = 30) { return this._get("/usage", { days }); }
  analytics(days = 30) { return this._get("/analytics", { days }); }
  plan() { return this._get("/plan"); }
  phoneNumbers() { return this._get("/phone-numbers").then((r) => r.items); }

  _get(path, params) { return this._request("GET", path, undefined, params); }
  _send(method, path, body) { return this._request(method, path, body); }

  async _request(method, path, body, params) {
    const qs = params ? `?${new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]))}` : "";
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), this._timeout);
    try {
      const r = await this._fetch(`${this.base}/api/v1${path}${qs}`, {
        method, signal: ctl.signal,
        headers: { Authorization: this._auth, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      if (!r.ok) {
        let detail;
        try { detail = (await r.json()).detail; } catch { detail = r.statusText; }
        throw new OmniVoiceError(r.status, typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      if (r.status === 204) return null;
      const text = await r.text();
      return text ? JSON.parse(text) : null;
    } finally {
      clearTimeout(timer);
    }
  }
}
