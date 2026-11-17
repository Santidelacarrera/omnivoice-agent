export class OmniVoiceError extends Error { status: number; detail: string }
export function verifyWebhook(secret: string, signatureHeader: string, body: string, opts?: { toleranceS?: number; now?: number }): boolean;
type Json = Record<string, any>;
export class OmniVoice {
  constructor(baseUrl: string, opts: { apiKey?: string; token?: string; timeoutMs?: number; fetch?: typeof fetch });
  agents: {
    list(): Promise<Json[]>; get(id: string): Promise<Json>;
    create(a: { name: string; instructions: string; tools?: string[]; voice?: string | null; language?: string }): Promise<Json>;
    update(id: string, changes: Json): Promise<Json>; versions(id: string): Promise<Json[]>; rollback(id: string, version: number): Promise<Json>;
    publish(id: string): Promise<Json>; archive(id: string): Promise<Json>; simulate(id: string, turns: string[]): Promise<Json>;
  };
  calls: {
    list(limit?: number): Promise<Json[]>; get(id: string): Promise<Json>;
    create(a: { to: string; consentBasis: "explicit_opt_in" | "existing_customer" | "transactional"; consentRef?: string; scheduledAt?: string; timezone?: string; maxAttempts?: number; fromNumber?: string; idempotencyKey?: string }): Promise<Json>;
    transfer(id: string, reason?: string): Promise<Json>; end(id: string): Promise<Json>;
  };
  outbound: { list(): Promise<Json[]>; get(id: string): Promise<Json>; cancel(id: string): Promise<Json>; doNotCall(number: string, reason?: string): Promise<Json> };
  webhooks: { list(): Promise<Json>; create(a: { url: string; events?: string[]; description?: string }): Promise<Json>; delete(id: string): Promise<null>; deliveries(id: string): Promise<Json[]> };
  team: { list(): Promise<Json[]>; set(email: string, role: string): Promise<Json>; remove(email: string): Promise<null> };
  usage(days?: number): Promise<Json>; analytics(days?: number): Promise<Json>; plan(): Promise<Json>; phoneNumbers(): Promise<Json[]>;
}
