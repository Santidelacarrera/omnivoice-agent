-- Clientes pedidos y confirmaciones: herramientas del agente para reconocer a quien llama y consultar sus pedidos.
-- Privacidad: no se guarda el número de teléfono (solo su huella corta, la misma que usa `calls`); el nombre es el único dato personal.
CREATE TABLE IF NOT EXISTS customers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  phone_hash TEXT NOT NULL,
  first_name TEXT NOT NULL CHECK (length(first_name) BETWEEN 1 AND 80),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, phone_hash)
);

-- Los pedidos los importa la empresa desde su sistema (PUT /api/v1/orders/{id}); el agente solo los lee, y solo los de quien llama.
CREATE TABLE IF NOT EXISTS orders (
  id TEXT NOT NULL CHECK (id ~ '^[A-Za-z0-9-]{4,30}$'),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  phone_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('received','preparing','shipped','delivered','cancelled')),
  summary TEXT NOT NULL CHECK (length(summary) <= 300),
  total NUMERIC(12,2),
  eta_date DATE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, id)
);
CREATE INDEX IF NOT EXISTS orders_phone_idx ON orders (org_id, phone_hash);

ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON customers USING (org_id = current_setting('app.org_id', true)::uuid);
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON orders USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON customers TO omni_app;   -- DELETE: derecho de supresión
GRANT SELECT, INSERT, UPDATE ON orders TO omni_app;
