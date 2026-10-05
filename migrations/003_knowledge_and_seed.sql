CREATE TABLE knowledge_base (
  id BIGSERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX idx_kb_fts ON knowledge_base USING gin (to_tsvector('spanish', title || ' ' || body));
ALTER TABLE knowledge_base ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_base FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON knowledge_base USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_base TO omni_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO omni_app;

-- Datos demo (organización fija para desarrollo)
INSERT INTO organizations (id, name) VALUES ('00000000-0000-0000-0000-000000000001', 'Demo Retail');

-- El seed corre como superusuario de migraciones, que omite RLS por diseño.
INSERT INTO inventory (org_id, product, color, size, units, price) VALUES
  ('00000000-0000-0000-0000-000000000001', 'chaqueta', 'negro', 'M', 4, 89.90),
  ('00000000-0000-0000-0000-000000000001', 'chaqueta', 'negro', 'L', 0, 89.90),
  ('00000000-0000-0000-0000-000000000001', 'chaqueta', 'azul', 'M', 7, 94.90);
INSERT INTO knowledge_base (org_id, title, body) VALUES
  ('00000000-0000-0000-0000-000000000001', 'Política de devoluciones', 'Aceptamos devoluciones dentro de 30 días con el ticket de compra y el producto sin uso.'),
  ('00000000-0000-0000-0000-000000000001', 'Horario de atención', 'Atendemos de lunes a sábado de 9:00 a 20:00.');
