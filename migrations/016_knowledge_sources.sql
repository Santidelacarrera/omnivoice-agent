-- Etapa 7: documentos de conocimiento con fuente y versión (la tabla `knowledge_base` de la migración 003 sigue siendo válida y se busca también).
-- Búsqueda: texto completo en español (sin embeddings: la búsqueda semántica queda para cuando se elija proveedor).
CREATE TABLE IF NOT EXISTS kb_sources (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  kind TEXT NOT NULL CHECK (kind IN ('txt','csv','docx','pdf')),
  version INT NOT NULL DEFAULT 1,
  bytes INT NOT NULL,
  chars INT NOT NULL,
  chunk_count INT NOT NULL,
  checksum TEXT NOT NULL,                         -- SHA-256 del archivo: detecta un documento idéntico subido otra vez
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kb_sources_org_idx ON kb_sources (org_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS kb_chunks (
  id BIGSERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  source_id UUID NOT NULL REFERENCES kb_sources(id) ON DELETE CASCADE,
  ordinal INT NOT NULL,
  body TEXT NOT NULL,
  tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('spanish', body)) STORED
);
CREATE INDEX IF NOT EXISTS kb_chunks_tsv_idx ON kb_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS kb_chunks_source_idx ON kb_chunks (source_id, ordinal);

ALTER TABLE kb_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON kb_sources USING (org_id = current_setting('app.org_id', true)::uuid);
ALTER TABLE kb_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON kb_chunks USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON kb_sources, kb_chunks TO omni_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO omni_app;
