-- Etapa 3: resolver a qué organización y agente pertenece un número ANTES de saber la organización.
-- Con RLS forzada la app no puede buscar un número sin org_id, así que se expone una función mínima
-- (SECURITY DEFINER, ejecutada con los derechos del dueño = rol de migraciones, que no está sujeto a RLS).
-- Solo devuelve org, agente y estado del agente para un E.164 exacto: no permite listar ni enumerar números.
CREATE OR REPLACE FUNCTION resolve_phone_number(p_e164 TEXT)
RETURNS TABLE (org_id UUID, agent_id UUID, agent_status TEXT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT p.org_id, p.agent_id, a.status
  FROM phone_numbers p LEFT JOIN agents a ON a.id = p.agent_id
  WHERE p.e164 = p_e164
$$;
REVOKE ALL ON FUNCTION resolve_phone_number(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_phone_number(TEXT) TO omni_app;
