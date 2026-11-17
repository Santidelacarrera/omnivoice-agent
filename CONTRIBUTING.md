# Contribuir

## Antes de empezar
Lee `docs/ARCHITECTURE.md` (decisiones y límites), `docs/SECURITY.md` y `docs/TESTING.md`. El estado real del proyecto y lo pendiente está en `OMNIVOICE_TECHNICAL_AUDIT.md` y `OMNIVOICE_IMPLEMENTATION_ROADMAP.md`.

## Entorno
```bash
cd backend && pip install -r requirements-dev.txt && python -m pytest -q --ignore=tests/integration
cd frontend && npm ci && npm run typecheck && npm run build
```
Las pruebas de integración necesitan PostgreSQL y Redis (instrucciones en `docs/TESTING.md`).

## Reglas del proyecto
1. **Una rama y un PR por cambio coherente.** Un PR muy grande no se puede revisar: divide por etapa o por módulo. Si un PR depende de otro, apílalo y dilo en la descripción.
2. **Pruebas con cada cambio.** Unitarias siempre; si toca la base de datos, también contra PostgreSQL real; si añade un recurso por organización, una prueba de aislamiento entre organizaciones. La cobertura de las unitarias no puede bajar del 80 %.
3. **Migraciones:** un archivo nuevo `NNN_descripcion.sql`; **nunca edites una migración ya aplicada** (el ejecutor detecta el cambio y se detiene). Haz las migraciones aplicables una sola vez; el ejecutor las envuelve en una transacción. Toda tabla con `org_id` lleva RLS forzada y política `org_isolation`.
4. **Seguridad:** el modelo propone, el backend dispone. Valida en el servidor, filtra por organización, no guardes datos personales ni argumentos de herramientas, no registres secretos. Una función `SECURITY DEFINER` solo para una operación acotada y documentada.
5. **Honestidad en la documentación:** no anuncies certificaciones ni garantías que no se hayan verificado; documenta los límites conocidos junto a cada control.
6. **Dependencias:** ejecuta `pip-audit -r requirements.txt` (o `npm audit`) antes de subir; el CI lo exige.
7. **Calidad:** `ruff check .` limpio. Comentarios y mensajes de usuario en español, como el resto del código.

## Commits y PR
Mensajes en español con el tipo de cambio (`feat`, `fix`, `test`, `docs`, `build`, `ci`) y el porqué. La descripción del PR indica cambios incompatibles, pasos de despliegue (migraciones, variables), cómo se verificó y qué **no** se verificó.

## Seguridad
Reporta vulnerabilidades en privado (ver `docs/SECURITY.md`), no en un issue público.
