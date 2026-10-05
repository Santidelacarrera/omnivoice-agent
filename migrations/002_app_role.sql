-- Rol de la aplicación SIN superusuario: los superusuarios y propietarios omiten RLS.
-- En producción cambia la contraseña (p. ej. con un secreto gestionado) antes de aplicar.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'omni_app') THEN
    CREATE ROLE omni_app LOGIN PASSWORD 'omni_app' NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

GRANT CONNECT ON DATABASE omnivoice TO omni_app;
GRANT USAGE ON SCHEMA public TO omni_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO omni_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO omni_app;

-- El propietario también queda sujeto a las políticas.
ALTER TABLE conversations FORCE ROW LEVEL SECURITY;
ALTER TABLE conversation_events FORCE ROW LEVEL SECURITY;
ALTER TABLE transcripts FORCE ROW LEVEL SECURITY;
ALTER TABLE tool_executions FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;
ALTER TABLE inventory FORCE ROW LEVEL SECURITY;
ALTER TABLE reservations FORCE ROW LEVEL SECURITY;
ALTER TABLE support_tickets FORCE ROW LEVEL SECURITY;

-- sessions y usage_records también aislados por organización.
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON sessions USING (org_id = current_setting('app.org_id', true)::uuid);
ALTER TABLE usage_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_records FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON usage_records USING (org_id = current_setting('app.org_id', true)::uuid);

-- agents y agent_tools también aislados por organización (agent_tools hereda la de su agente).
ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE agents FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON agents USING (org_id = current_setting('app.org_id', true)::uuid);
ALTER TABLE agent_tools ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tools FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON agent_tools USING (
  EXISTS (SELECT 1 FROM agents a WHERE a.id = agent_id AND a.org_id = current_setting('app.org_id', true)::uuid)
);

-- La app no gestiona identidades: sin acceso a credenciales ni a membresías de otras organizaciones.
REVOKE ALL ON users, memberships FROM omni_app;
REVOKE ALL ON organizations FROM omni_app;
GRANT SELECT (id, name, retention_days) ON organizations TO omni_app;

-- Auditoría append-only: la app puede insertar y leer, nunca modificar ni borrar.
REVOKE UPDATE, DELETE ON audit_logs FROM omni_app;
