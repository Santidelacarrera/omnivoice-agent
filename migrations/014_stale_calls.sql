-- Llamadas abandonadas: las que quedaron sin estado final (el proveedor dejó de avisar o cayó una réplica).
-- El vigilante no pertenece a ninguna organización, así que la búsqueda es una función SECURITY DEFINER acotada a esta consulta
-- (solo devuelve organización, id de llamada y estado; mismo patrón que 006/007/008/011/012/013).
CREATE OR REPLACE FUNCTION find_stale_calls(p_now TIMESTAMPTZ, p_pre_answer_s INT, p_live_s INT, p_limit INT)
RETURNS TABLE (org_id UUID, provider TEXT, sid TEXT, status TEXT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT c.org_id, c.provider, c.provider_call_sid, c.status
  FROM calls c
  WHERE c.status NOT IN ('COMPLETED','FAILED','BUSY','NO_ANSWER','CANCELLED','TRANSFERRED')
    AND COALESCE((SELECT max(e.ts) FROM call_events e WHERE e.call_id = c.id), c.created_at)
        < p_now - make_interval(secs => CASE WHEN c.status IN ('ANSWERED','CONNECTED') THEN p_live_s ELSE p_pre_answer_s END)
  ORDER BY c.created_at
  LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION find_stale_calls(TIMESTAMPTZ, INT, INT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION find_stale_calls(TIMESTAMPTZ, INT, INT, INT) TO omni_app;
