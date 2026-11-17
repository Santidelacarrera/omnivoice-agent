-- Etapa 8/10: claves de API por organización. Solo se guarda el hash; la clave completa se muestra una vez al crearla.
CREATE TABLE IF NOT EXISTS api_keys (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  prefix TEXT NOT NULL UNIQUE,                       -- identifica la clave sin revelarla (ovk_<prefix>_<secreto>)
  key_hash TEXT NOT NULL,                            -- SHA-256 de la clave completa (alta entropía: no necesita sal ni KDF lento)
  role TEXT NOT NULL CHECK (role IN ('admin','manager','operator','analyst','viewer')),  -- nunca 'owner'
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS api_keys_org_idx ON api_keys (org_id, created_at DESC);
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON api_keys USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON api_keys TO omni_app;   -- sin DELETE: las claves se revocan, no se borran

-- Al autenticar solo se conoce la clave, no la organización: búsqueda exacta por prefijo (misma técnica que 006/007).
CREATE OR REPLACE FUNCTION resolve_api_key(p_prefix TEXT)
RETURNS TABLE (id UUID, org_id UUID, key_hash TEXT, role TEXT, expires_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$ SELECT k.id, k.org_id, k.key_hash, k.role, k.expires_at, k.revoked_at FROM api_keys k WHERE k.prefix = p_prefix $$;
REVOKE ALL ON FUNCTION resolve_api_key(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_api_key(TEXT) TO omni_app;
