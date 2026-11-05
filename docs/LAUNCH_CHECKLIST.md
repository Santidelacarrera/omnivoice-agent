# Lista de comprobación de lanzamiento

Hecho en el repositorio:
- [x] Producción no arranca insegura (JWT ≥ 32, Postgres + Redis, CORS explícito, sin `dev-token`).
- [x] Compose de producción: solo Caddy expuesto, TLS automático, secretos obligatorios, contenedores sin root.
- [x] Clave real del rol `omni_app` en el primer arranque; RLS verificada en CI.
- [x] Escaneo de dependencias (`pip-audit`, `npm audit`) y lint en CI; construcción de imágenes en CI.
- [x] Copia diaria automática (servicio `backup`, con comprobación de integridad y rotación), `scripts/backup.sh`, `scripts/restore.sh` y alta de organizaciones (`scripts/create_org.sh`).
- [x] Alertas de Prometheus cargadas (`infrastructure/alerts.yml`): backend caído, latencia, barge-in, errores, persistencia, proveedor, topes.
- [x] Emisión de JWT para operadores: `docker compose exec backend python -m app.cli issue-token --org <uuid> --user <id> --role admin`.
- [x] Workflow `release.yml`: al etiquetar `vX.Y.Z` construye, escanea con Trivy (bloquea HIGH/CRITICAL) y publica en GHCR. Dependabot cubre pip, npm, Docker y Actions.

Antes de abrir al público (requiere tu infraestructura):
- [ ] Servidor con Docker, DNS `A` de `APP_DOMAIN` y `API_DOMAIN`, firewall con 22/80/443.
- [ ] `.env.production` con secretos propios y `OPENAI_API_KEY`/`GEMINI_API_KEY` con crédito.
- [ ] Primer `up -d --build` y comprobar `https://API_DOMAIN/readyz`.
- [ ] Ensayar `scripts/backup.sh` y una **restauración** en otra máquina (`gunzip -c f.sql.gz | psql`).
- [ ] Ejecutar `backend/bench/run_real.sh` contra los proveedores reales y revisar `docs/slo.json`.
- [ ] Conectar tu proveedor de identidad (emite el JWT con `sub`, `org`, `role`) o usar `app.cli issue-token`, y crear organizaciones.
- [ ] Conectar Alertmanager (o Grafana) de las alertas a tu canal de guardia.
- [ ] Proteger `main` en GitHub con los checks `backend`, `migrations`, `frontend`, `docker`.
