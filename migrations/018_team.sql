-- Equipo: la aplicación no puede leer `users` ni `memberships` (migración 002), así que gestionar el equipo desde el panel se hace con
-- funciones SECURITY DEFINER, cada una acotada a UNA organización (la que pasa la aplicación a partir del token ya autenticado) y con las reglas
-- de negocio dentro de la propia función, para que no dependan de que el código de la API las recuerde:
--   * solo un owner puede dar, quitar o cambiar el rol owner;  * nunca se deja a una organización sin owner;  * roles válidos solamente.
-- La plataforma NO autentica a nadie con esta tabla: el token (JWT) lo emite un proveedor de identidad externo, que debe consultarla.
CREATE OR REPLACE FUNCTION team_list(p_org UUID)
RETURNS TABLE (user_id UUID, email TEXT, role TEXT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$ SELECT u.id, u.email, m.role FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.org_id = p_org ORDER BY u.email $$;

CREATE OR REPLACE FUNCTION team_set_member(p_org UUID, p_email TEXT, p_role TEXT, p_actor_role TEXT)
RETURNS TABLE (user_id UUID, role TEXT, previous TEXT)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
#variable_conflict use_column
DECLARE v_uid UUID; v_prev TEXT; v_owners INT;
BEGIN
  IF p_role NOT IN ('owner','admin','manager','operator','analyst','viewer') THEN RAISE EXCEPTION 'invalid_role'; END IF;
  IF p_actor_role NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
  IF p_email !~ '^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$' THEN RAISE EXCEPTION 'invalid_email'; END IF;
  PERFORM 1 FROM organizations WHERE id = p_org FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'org_not_found'; END IF;
  INSERT INTO users (email) VALUES (lower(p_email)) ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id INTO v_uid;
  SELECT m.role INTO v_prev FROM memberships m WHERE m.user_id = v_uid AND m.org_id = p_org;
  IF (p_role = 'owner' OR v_prev = 'owner') AND p_actor_role <> 'owner' THEN RAISE EXCEPTION 'owner_only'; END IF;
  IF v_prev = 'owner' AND p_role <> 'owner' THEN
    SELECT count(*) INTO v_owners FROM memberships WHERE org_id = p_org AND role = 'owner';
    IF v_owners <= 1 THEN RAISE EXCEPTION 'last_owner'; END IF;
  END IF;
  INSERT INTO memberships (user_id, org_id, role) VALUES (v_uid, p_org, p_role)
    ON CONFLICT (user_id, org_id) DO UPDATE SET role = EXCLUDED.role;
  INSERT INTO audit_logs (org_id, actor, action, detail)
    VALUES (p_org, 'team', 'team.member_set', jsonb_build_object('user_id', v_uid, 'role', p_role, 'previous', v_prev));
  RETURN QUERY SELECT v_uid, p_role, v_prev;
END $$;

CREATE OR REPLACE FUNCTION team_remove_member(p_org UUID, p_email TEXT, p_actor_role TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE v_uid UUID; v_role TEXT; v_owners INT;
BEGIN
  IF p_actor_role NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
  SELECT u.id, m.role INTO v_uid, v_role FROM users u JOIN memberships m ON m.user_id = u.id AND m.org_id = p_org WHERE u.email = lower(p_email);
  IF v_uid IS NULL THEN RETURN FALSE; END IF;
  IF v_role = 'owner' THEN
    IF p_actor_role <> 'owner' THEN RAISE EXCEPTION 'owner_only'; END IF;
    PERFORM 1 FROM organizations WHERE id = p_org FOR UPDATE;
    SELECT count(*) INTO v_owners FROM memberships WHERE org_id = p_org AND role = 'owner';
    IF v_owners <= 1 THEN RAISE EXCEPTION 'last_owner'; END IF;
  END IF;
  DELETE FROM memberships WHERE user_id = v_uid AND org_id = p_org;
  INSERT INTO audit_logs (org_id, actor, action, detail) VALUES (p_org, 'team', 'team.member_removed', jsonb_build_object('user_id', v_uid, 'role', v_role));
  RETURN TRUE;
END $$;

REVOKE ALL ON FUNCTION team_list(UUID), team_set_member(UUID, TEXT, TEXT, TEXT), team_remove_member(UUID, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION team_list(UUID), team_set_member(UUID, TEXT, TEXT, TEXT), team_remove_member(UUID, TEXT, TEXT) TO omni_app;
