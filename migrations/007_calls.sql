-- Etapa 3: registro de llamadas y sus eventos, idempotente por proveedor + id de llamada.
CREATE TABLE IF NOT EXISTS calls (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  provider_call_sid TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
  agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
  our_number TEXT,
  peer_hash TEXT,                       -- hash corto del otro extremo: el número es dato personal
  status TEXT NOT NULL DEFAULT 'CREATED' CHECK (status IN
    ('CREATED','QUEUED','RINGING','ANSWERED','CONNECTED','COMPLETED','FAILED','BUSY','NO_ANSWER','CANCELLED','TRANSFERRED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  answered_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ,
  UNIQUE (provider, provider_call_sid)  -- una llamada = una fila, aunque el proveedor reintente el webhook
);
CREATE INDEX IF NOT EXISTS calls_org_created_idx ON calls (org_id, created_at DESC);

CREATE TABLE IF NOT EXISTS call_events (
  id BIGSERIAL PRIMARY KEY,
  call_id UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  event_key TEXT NOT NULL,              -- identifica el evento del proveedor: repetirlo no tiene efecto
  applied BOOLEAN NOT NULL,             -- false = llegó tarde o desordenado y no cambió el estado
  detail JSONB NOT NULL DEFAULT '{}',
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (call_id, event_key)
);

ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE calls FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON calls USING (org_id = current_setting('app.org_id', true)::uuid);
ALTER TABLE call_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE call_events FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON call_events USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON calls TO omni_app;
GRANT SELECT, INSERT ON call_events TO omni_app;   -- el historial de eventos no se modifica ni se borra
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO omni_app;

-- Los avisos de estado del proveedor solo traen el id de llamada, no la organización: búsqueda exacta mínima (ver 006).
CREATE OR REPLACE FUNCTION resolve_call_org(p_provider TEXT, p_sid TEXT)
RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$ SELECT org_id FROM calls WHERE provider = p_provider AND provider_call_sid = p_sid $$;
REVOKE ALL ON FUNCTION resolve_call_org(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_call_org(TEXT, TEXT) TO omni_app;
