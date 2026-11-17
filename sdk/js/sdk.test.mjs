import test from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { OmniVoice, OmniVoiceError, verifyWebhook } from "./omnivoice-sdk.mjs";

function fake(status, payload) {
  const calls = [];
  const f = async (url, init) => {
    calls.push({ url, init });
    return { ok: status < 400, status, statusText: "x", json: async () => payload, text: async () => (payload === undefined ? "" : JSON.stringify(payload)) };
  };
  return { f, calls };
}

test("crea llamadas con cuerpo en snake_case, sin campos vacíos y con la clave en Authorization", async () => {
  const { f, calls } = fake(201, { id: "j1" });
  const ov = new OmniVoice("https://x.test/", { apiKey: "ovk_a", fetch: f });
  assert.deepEqual(await ov.calls.create({ to: "+56987654321", consentBasis: "existing_customer" }), { id: "j1" });
  assert.equal(calls[0].url, "https://x.test/api/v1/calls");
  assert.equal(calls[0].init.headers.Authorization, "Bearer ovk_a");
  assert.deepEqual(JSON.parse(calls[0].init.body), { to: "+56987654321", consent_basis: "existing_customer" });
});

test("listas, parámetros, 204 y errores de la API", async () => {
  let { f, calls } = fake(200, { items: [1, 2] });
  assert.deepEqual(await new OmniVoice("https://x.test", { apiKey: "k", fetch: f }).calls.list(5), [1, 2]);
  assert.match(calls[0].url, /\/calls\?limit=5$/);
  ({ f } = fake(204, undefined));
  assert.equal(await new OmniVoice("https://x.test", { apiKey: "k", fetch: f }).team.remove("a@b.co"), null);
  ({ f } = fake(409, { detail: "La organización no puede quedarse sin owner" }));
  await assert.rejects(new OmniVoice("https://x.test", { apiKey: "k", fetch: f }).team.set("a@b.co", "admin"),
    (e) => e instanceof OmniVoiceError && e.status === 409 && /sin owner/.test(e.detail));
  assert.throws(() => new OmniVoice("https://x.test", {}), TypeError);
});

test("verifyWebhook valida firma, cuerpo y tolerancia", () => {
  const secret = "s3cret", body = '{"a":1}', t = 1_700_000_000;
  const sig = `t=${t},v1=${createHmac("sha256", secret).update(`${t}.${body}`).digest("hex")}`;
  assert.equal(verifyWebhook(secret, sig, body, { now: t + 10 }), true);
  assert.equal(verifyWebhook(secret, sig, body + " ", { now: t + 10 }), false);
  assert.equal(verifyWebhook("otro", sig, body, { now: t + 10 }), false);
  assert.equal(verifyWebhook(secret, sig, body, { now: t + 1000 }), false);
  assert.equal(verifyWebhook(secret, "basura", body), false);
});
