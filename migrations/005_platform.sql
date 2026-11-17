-- Etapa 2: roles completos, agentes versionados y números telefónicos.
ALTER TABLE memberships DROP CONSTRAINT IF EXISTS memberships_role_check;
ALTER TABLE memberships ADD CONSTRAINT memberships_role_check
  CHECK (role IN ('owner','admin','manager','operator','analyst','viewer','customer'));

ALTER TABLE agents ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft'
  CHECK (status IN ('draft','published','archived'));
ALTER TABLE agents ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS agent_versions (
  agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  version INT NOT NULL,
  snapshot JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, version)
);
ALTER TABLE agent_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON agent_versions USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT ON agent_versions TO omni_app;  -- historial inmutable

-- Un número pertenece a una sola organización en toda la plataforma (UNIQUE global).
CREATE TABLE IF NOT EXISTS phone_numbers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  e164 TEXT NOT NULL UNIQUE CHECK (e164 ~ '^\+[1-9][0-9]{6,14}$'),
  agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS phone_numbers_org_idx ON phone_numbers (org_id);
ALTER TABLE phone_numbers ENABLE ROW LEVEL SECURITY;
ALTER TABLE phone_numbers FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON phone_numbers USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON phone_numbers TO omni_app;

-- Los agentes previos a esta migración reciben su instantánea inicial.
INSERT INTO agent_versions (agent_id, org_id, version, snapshot)
SELECT a.id, a.org_id, a.version,
       jsonb_build_object('version', a.version, 'name', a.name, 'instructions', a.instructions, 'voice', a.voice,
                          'language', a.language,
                          'tools', COALESCE((SELECT jsonb_agg(t.tool_name ORDER BY t.tool_name) FROM agent_tools t
                                             WHERE t.agent_id = a.id AND t.enabled), '[]'::jsonb))
FROM agents a ON CONFLICT DO NOTHING;
