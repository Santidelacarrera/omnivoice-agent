-- Plan de cada organización (los límites viven en la configuración, no en la base: ver app/billing/plans.py).
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'starter'
  CHECK (plan IN ('starter','business','professional','enterprise'));
GRANT SELECT (plan) ON organizations TO omni_app;   -- la aplicación solo lee el plan; cambiarlo es del operador (CLI)
