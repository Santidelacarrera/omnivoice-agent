-- Purga de entregas de webhooks terminadas. La ejecuta el trabajador, que no pertenece a ninguna organización,
-- así que es una función SECURITY DEFINER acotada a esta única operación (misma técnica que 006/007/008/011/012).
CREATE OR REPLACE FUNCTION purge_webhook_deliveries(p_cutoff TIMESTAMPTZ)
RETURNS INT
LANGUAGE sql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  WITH d AS (DELETE FROM webhook_deliveries WHERE status IN ('delivered','failed') AND created_at < p_cutoff RETURNING 1)
  SELECT count(*)::int FROM d
$$;
REVOKE ALL ON FUNCTION purge_webhook_deliveries(TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_webhook_deliveries(TIMESTAMPTZ) TO omni_app;
