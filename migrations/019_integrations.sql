-- Integraciones: conectores HTTP definidos por la organización (CRM, agenda, ERP propios...) que el agente puede consultar o accionar.
-- Las credenciales se guardan cifradas (Fernet, como el secreto de los webhooks) y la API nunca las devuelve.
-- Cada conector limita a qué métodos y rutas puede llamar el agente: el modelo no puede salir de esa lista.
CREATE TABLE IF NOT EXISTS integrations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name TEXT NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{1,39}$'),
  base_url TEXT NOT NULL CHECK (length(base_url) <= 500),
  auth_type TEXT NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none','bearer','header','basic','oauth2_client_credentials')),
  auth_header TEXT CHECK (auth_header IS NULL OR auth_header ~ '^[A-Za-z0-9-]{1,60}$'),
  token_url TEXT CHECK (token_url IS NULL OR length(token_url) <= 500),
  scope TEXT CHECK (scope IS NULL OR length(scope) <= 200),
  credential_enc TEXT,
  allowed_methods TEXT[] NOT NULL DEFAULT ARRAY['GET'] CHECK (allowed_methods <@ ARRAY['GET','POST','PUT','PATCH','DELETE']),
  allowed_paths TEXT[] NOT NULL DEFAULT ARRAY['/'],
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, name)
);
ALTER TABLE integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE integrations FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON integrations USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON integrations TO omni_app;
