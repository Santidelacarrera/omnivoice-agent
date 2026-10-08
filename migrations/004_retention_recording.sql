-- Retención, consentimiento y grabaciones.
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS recording_enabled BOOLEAN NOT NULL DEFAULT false;
GRANT SELECT (recording_enabled) ON organizations TO omni_app;

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS recording_consent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS consent_at TIMESTAMPTZ;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS voice TEXT;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS language TEXT;
CREATE INDEX IF NOT EXISTS conversations_org_created_idx ON conversations (org_id, created_at);

CREATE TABLE IF NOT EXISTS recordings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  storage_key TEXT NOT NULL,
  bytes BIGINT NOT NULL,
  duration_s NUMERIC(10,2) NOT NULL,
  format TEXT NOT NULL DEFAULT 'wav',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS recordings_conversation_idx ON recordings (conversation_id);

ALTER TABLE recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE recordings FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON recordings USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON recordings TO omni_app;
