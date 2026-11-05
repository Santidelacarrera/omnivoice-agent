# Lista de comprobación de lanzamiento

Hecho en el repositorio:
- [x] Producción no arranca insegura (JWT ≥ 32, Postgres + Redis, CORS explícito, sin `dev-token`).
- [x] Compose de producción: solo Caddy expuesto, TLS automático, secretos obligatorios, contenedores sin root.
- [x] Clave real del rol `omni_app` en el primer arranque; RLS verificada en CI.
- [x] Escaneo de dependencias (`pip-audit`, `npm audit`) y lint en CI; construcción de imágenes en CI.
- [x] Copia de seguridad (`scripts/backup.sh`) y alta de organizaciones (`scripts/create_org.sh`).

Antes de abrir al público (requiere tu infraestructura):
- [ ] Servidor con Docker, DNS `A` de `APP_DOMAIN` y `API_DOMAIN`, firewall con 22/80/443.
- [ ] `.env.production` con secretos propios y `OPENAI_API_KEY`/`GEMINI_API_KEY` con crédito.
- [ ] Primer `up -d --build` y comprobar `https://API_DOMAIN/readyz`.
- [ ] Ensayar `scripts/backup.sh` y una **restauración** en otra máquina (`gunzip -c f.sql.gz | psql`).
- [ ] Ejecutar `backend/bench/run_real.sh` contra los proveedores reales y revisar `docs/slo.json`.
- [ ] Conectar tu proveedor de identidad (emite el JWT con `sub`, `org`, `role`) y crear organizaciones.
- [ ] Alertas de Prometheus de `docs/RUNBOOK.md` hacia tu canal de guardia.
- [ ] Proteger `main` en GitHub con los checks `backend`, `migrations`, `frontend`, `docker`.
