-- Etapa 3: llamadas salientes controladas (consentimiento, horarios, reintentos, lista de exclusión).
CREATE TABLE IF NOT EXISTS outbound_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  number_hash TEXT NOT NULL,                       -- huella con sal por organización (duplicados y exclusión)
  to_number TEXT,                                  -- solo mientras el trabajo está activo; se borra al terminar
  from_number TEXT NOT NULL,
  timezone TEXT NOT NULL,
  consent_basis TEXT NOT NULL CHECK (consent_basis IN ('explicit_opt_in','existing_customer','transactional')),
  consent_ref TEXT,
  idempotency_key TEXT,
  status TEXT NOT NULL DEFAULT 'SCHEDULED'
    CHECK (status IN ('SCHEDULED','DIALING','COMPLETED','EXHAUSTED','CANCELLED','FAILED')),
  attempts INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
  next_attempt_at TIMESTAMPTZ NOT NULL,
  claimed_at TIMESTAMPTZ,
  last_call_sid TEXT,
  last_result TEXT,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  CHECK (status NOT IN ('SCHEDULED','DIALING') OR to_number IS NOT NULL)
);
-- Un número no puede tener dos trabajos activos a la vez, ni siquiera con peticiones simultáneas.
CREATE UNIQUE INDEX IF NOT EXISTS outbound_jobs_active_number_uq ON outbound_jobs (org_id, number_hash)
  WHERE status IN ('SCHEDULED','DIALING');
CREATE UNIQUE INDEX IF NOT EXISTS outbound_jobs_idem_uq ON outbound_jobs (org_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS outbound_jobs_due_idx ON outbound_jobs (next_attempt_at) WHERE status IN ('SCHEDULED','DIALING');
CREATE INDEX IF NOT EXISTS outbound_jobs_call_idx ON outbound_jobs (org_id, last_call_sid);

CREATE TABLE IF NOT EXISTS do_not_call (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  number_hash TEXT NOT NULL,                       -- no se guarda el número: solo hace falta compararlo
  reason TEXT,
  added_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, number_hash)
);

ALTER TABLE outbound_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbound_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON outbound_jobs USING (org_id = current_setting('app.org_id', true)::uuid);
ALTER TABLE do_not_call ENABLE ROW LEVEL SECURITY;
ALTER TABLE do_not_call FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON do_not_call USING (org_id = current_setting('app.org_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON outbound_jobs TO omni_app;       -- sin DELETE: el registro de intentos se conserva
GRANT SELECT, INSERT, DELETE ON do_not_call TO omni_app;

-- El planificador no pertenece a ninguna organización: toma trabajos vencidos de todas, de forma atómica y sin pisarse
-- entre réplicas (SKIP LOCKED). Recupera también los que quedaron "marcando" (réplica caída) tras `p_stale_s` segundos.
CREATE OR REPLACE FUNCTION claim_due_outbound_jobs(p_now TIMESTAMPTZ, p_limit INT, p_stale_s INT)
RETURNS TABLE (id UUID, org_id UUID, number_hash TEXT, to_number TEXT, from_number TEXT, timezone TEXT, attempts INT, max_attempts INT)
LANGUAGE sql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  UPDATE outbound_jobs j SET status = 'DIALING', claimed_at = p_now
  WHERE j.id IN (
    SELECT o.id FROM outbound_jobs o
    WHERE (o.status = 'SCHEDULED' AND o.next_attempt_at <= p_now)
       OR (o.status = 'DIALING' AND o.claimed_at < p_now - make_interval(secs => p_stale_s))
    ORDER BY o.next_attempt_at LIMIT p_limit FOR UPDATE SKIP LOCKED)
  RETURNING j.id, j.org_id, j.number_hash, j.to_number, j.from_number, j.timezone, j.attempts, j.max_attempts
$$;
REVOKE ALL ON FUNCTION claim_due_outbound_jobs(TIMESTAMPTZ, INT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_due_outbound_jobs(TIMESTAMPTZ, INT, INT) TO omni_app;
