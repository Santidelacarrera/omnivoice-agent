CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE organizations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  retention_days INT NOT NULL DEFAULT 30,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('admin','operator','customer')),
  PRIMARY KEY (user_id, org_id)
);

CREATE TABLE agents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  instructions TEXT NOT NULL,
  voice TEXT,
  language TEXT DEFAULT 'es',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_tools (
  agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  tool_name TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  policy JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (agent_id, tool_name)
);

CREATE TABLE sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id UUID REFERENCES users(id),
  agent_id UUID REFERENCES agents(id),
  state TEXT NOT NULL DEFAULT 'IDLE',
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ
);

CREATE TABLE conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  final_state TEXT,
  first_audio_ms INT,
  interruptions INT NOT NULL DEFAULT 0,
  lost_packets INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversation_events (
  id BIGSERIAL PRIMARY KEY,
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  org_id UUID NOT NULL,
  type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  correlation_id TEXT,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transcripts (
  id BIGSERIAL PRIMARY KEY,
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  org_id UUID NOT NULL,
  speaker TEXT NOT NULL CHECK (speaker IN ('user','agent')),
  text TEXT NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tool_executions (
  id BIGSERIAL PRIMARY KEY,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  org_id UUID NOT NULL,
  tool_name TEXT NOT NULL,
  call_id TEXT NOT NULL,
  args JSONB NOT NULL,
  result JSONB,
  ok BOOLEAN,
  duration_ms INT,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, call_id, tool_name)  -- idempotencia de escrituras
);

CREATE TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY,
  org_id UUID,
  actor TEXT,
  action TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}',
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE usage_records (
  id BIGSERIAL PRIMARY KEY,
  org_id UUID NOT NULL,
  session_id UUID,
  audio_seconds NUMERIC(10,2) NOT NULL DEFAULT 0,
  estimated_cost_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dominio de negocio de ejemplo
CREATE TABLE inventory (
  org_id UUID NOT NULL, product TEXT NOT NULL, color TEXT, size TEXT,
  units INT NOT NULL DEFAULT 0, price NUMERIC(10,2),
  PRIMARY KEY (org_id, product, color, size)
);
CREATE TABLE reservations (
  id TEXT PRIMARY KEY, org_id UUID NOT NULL, name TEXT NOT NULL,
  date DATE NOT NULL, time TIME NOT NULL, party_size INT NOT NULL CHECK (party_size BETWEEN 1 AND 20)
);
CREATE TABLE support_tickets (
  id TEXT PRIMARY KEY, org_id UUID NOT NULL, subject TEXT NOT NULL,
  description TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sessions_org ON sessions(org_id, started_at DESC);
CREATE INDEX idx_conv_org ON conversations(org_id, created_at DESC);
CREATE INDEX idx_events_conv ON conversation_events(conversation_id, ts);
CREATE INDEX idx_audit_org ON audit_logs(org_id, ts DESC);

-- Aislamiento entre organizaciones (RLS). La app fija: SET app.org_id = '<uuid>' por conexión/transacción.
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory ENABLE ROW LEVEL SECURITY;
ALTER TABLE reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_tickets ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['conversations','conversation_events','transcripts','tool_executions','audit_logs','inventory','reservations','support_tickets']
  LOOP
    EXECUTE format('CREATE POLICY org_isolation ON %I USING (org_id = current_setting(''app.org_id'', true)::uuid)', t);
  END LOOP;
END $$;
