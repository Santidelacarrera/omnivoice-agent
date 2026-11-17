-- Etapa 10: webhooks salientes (cola de entregas en la base de datos, secreto cifrado).
CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  url TEXT NOT NULL CHECK (length(url) <= 2000),
  secret_enc TEXT NOT NULL,                        -- secreto de firma cifrado con la clave de la aplicación (Fernet)
  events TEXT[] NOT NULL DEFAULT ARRAY['*'],
  description TEXT,
  active BOOLEAN NOT NULL DEFAULT true,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS webhook_endpoints_org_idx ON webhook_endpoints (org_id);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id BIGSERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  endpoint_id UUID NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
  event_id UUID NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,                          -- solo identificadores, estados y duraciones; sin datos personales
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','delivered','failed')),
  attempts INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at TIMESTAMPTZ,
  last_status INT,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at TIMESTAMPTZ,
  UNIQUE (endpoint_id, event_id)
);
CREATE INDEX IF NOT EXISTS webhook_deliveries_due_idx ON webhook_deliveries (next_attempt_at) WHERE status IN ('pending','sending');
CREATE INDEX IF NOT EXISTS webhook_deliveries_endpoint_idx ON webhook_deliveries (endpoint_id, created_at DESC);

ALTER TABLE webhook_endpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_endpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON webhook_endpoints USING (org_id = current_setting('app.org_id', true)::uuid);
ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON webhook_deliveries USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_endpoints TO omni_app;
GRANT SELECT, INSERT, UPDATE ON webhook_deliveries TO omni_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO omni_app;

-- El trabajador de envío no pertenece a ninguna organización: toma entregas vencidas de todas, de forma atómica
-- (SKIP LOCKED) y recupera las que quedaron "enviando" tras `p_stale_s` segundos (réplica caída). Devuelve lo necesario para enviar.
CREATE OR REPLACE FUNCTION claim_due_webhook_deliveries(p_now TIMESTAMPTZ, p_limit INT, p_stale_s INT)
RETURNS TABLE (id BIGINT, org_id UUID, endpoint_id UUID, url TEXT, secret_enc TEXT, event_id UUID, event_type TEXT, payload JSONB, attempts INT)
LANGUAGE sql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  WITH picked AS (
    SELECT d.id FROM webhook_deliveries d
    WHERE (d.status = 'pending' AND d.next_attempt_at <= p_now)
       OR (d.status = 'sending' AND d.claimed_at < p_now - make_interval(secs => p_stale_s))
    ORDER BY d.next_attempt_at LIMIT p_limit FOR UPDATE SKIP LOCKED
  ), upd AS (
    UPDATE webhook_deliveries w SET status = 'sending', claimed_at = p_now
    FROM picked WHERE w.id = picked.id
    RETURNING w.id, w.org_id, w.endpoint_id, w.event_id, w.event_type, w.payload, w.attempts
  )
  SELECT u.id, u.org_id, u.endpoint_id, e.url, e.secret_enc, u.event_id, u.event_type, u.payload, u.attempts
  FROM upd u JOIN webhook_endpoints e ON e.id = u.endpoint_id
$$;
REVOKE ALL ON FUNCTION claim_due_webhook_deliveries(TIMESTAMPTZ, INT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_due_webhook_deliveries(TIMESTAMPTZ, INT, INT) TO omni_app;
