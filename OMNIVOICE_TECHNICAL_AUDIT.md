# OmniVoice Agent — Auditoría técnica (2026-11-17)

Alcance: repositorio local en la rama `claude/omnivoice-multi-agent-007734` (base `a5cb180`). Evidencia: lectura de código, esquema SQL y ejecución de `pytest`
(116 pasan, 8 se omiten por requerir servicios reales; los de integración con PostgreSQL/Redis corren en CI).

Leyenda: ✅ Implementado y funcional · 🟡 Parcial · ⬜ Pendiente · 🔴 Defectuoso · 🔧 Necesita refactor · 🧪 Requiere pruebas adicionales

Hallazgo principal: el proyecto **no parte de cero**. El motor de voz, la telefonía básica, la seguridad por RLS, la observabilidad y la privacidad ya existen.
Lo que falta es la **capa de plataforma SaaS** (gestión de agentes con versiones, números, roles completos, API keys, facturación, integraciones, base de conocimiento real).

## 5.1 Estructura
| Componente | Estado | Evidencia / nota |
|---|---|---|
| Monolito modular FastAPI (`core`, `realtime`, `orchestration`, `telephony`, `tools`, `security`, `services`, `observability`) | ✅ | Separación clara; sin microservicios innecesarios |
| Frontend Next.js (agents, conversation, dashboard, history, settings) | 🟡 | 5 páginas; sin organizaciones, números, KB, integraciones, equipo, facturación ni auditoría |
| Docker / compose dev y prod, Caddy TLS | ✅ | `docker-compose*.yml`, `infrastructure/Caddyfile` |
| Documentación | 🟡 | Existen ARCHITECTURE, RUNBOOK, COSTS, LATENCY, PRIVACIDAD. Faltan API_DOCUMENTATION, DATABASE_SCHEMA, SECURITY, TESTING, BILLING, CONTRIBUTING, CHANGELOG |

## 5.2 Backend
| Componente | Estado | Nota |
|---|---|---|
| Endpoints (sesiones, conversaciones, agentes, métricas, auditoría, grabación) | ✅ | Versionados `/api/v1` |
| Agentes: solo listar y crear | 🟡 | Sin PATCH, versiones, archivar, duplicar, publicar, ni número asociado |
| Autenticación JWT con claims obligatorios | ✅ | Emisor externo; sin alta de usuarios, MFA ni recuperación (⬜) |
| Autorización | 🟡 | 3 roles (`admin`, `operator`, `customer`); la especificación exige Owner/Admin/Manager/Operator/Analyst/Viewer |
| Validación (Pydantic `extra=forbid`) y errores | ✅ | |
| Rate limiting y cupo de sesiones por organización | ✅ | Redis; ventana fija (límite documentado) |
| API keys públicas, webhooks salientes | ⬜ | |
| Llamadas (`/api/v1/calls`, transfer, end) | 🟡 | Solo `POST /api/v1/telephony/calls`; sin listado, estados completos ni transferencia |
| WebSocket de audio con tickets de un uso y control de Origin | ✅ | |

## 5.3 Motor de voz
| Componente | Estado | Nota |
|---|---|---|
| Proveedores OpenAI Realtime, Gemini Live, cascada Deepgram+Anthropic tras `RealtimeProvider` | ✅ | Capa de abstracción cumplida |
| VAD (energía), fin de turno, barge-in en dos niveles, épocas | ✅ | VAD por energía: 🧪 mal en ruido (límite documentado) |
| Reconexión acotada del proveedor | ✅ | ADR-8 |
| Medición de latencia por etapa y SLO | ✅ | `bench/`, `docs/slo.json` |
| Pruebas con proveedores reales | 🧪 | Contratos existen; ejecución real manual (`run_real.sh`) |

## 5.4 Datos e infraestructura
| Componente | Estado | Nota |
|---|---|---|
| PostgreSQL + RLS forzada, rol `omni_app` sin bypass | ✅ | Verificado en CI |
| Migraciones | 🟡 | SQL numerado a mano, sin herramienta ni tabla de versiones (🔧) |
| Entidades de la sección 20 | 🟡 | Existen: organizations, users, memberships, agents, agent_tools, sessions, conversations, conversation_events, transcripts, tool_executions, audit_logs, usage_records, recordings, knowledge_base. **Faltan:** roles/permissions (en código), agent_versions, phone_numbers, telephony_providers, calls, call_events, integrations, knowledge_sources, subscriptions, invoices, api_keys |
| Redis (tickets, rate limit, cupos) | ✅ | |
| Prometheus, alertas, Grafana | 🟡 | Métricas y `alerts.yml` ✅; dashboards de Grafana ⬜; OpenTelemetry ⬜; Sentry ⬜ |
| Copias de seguridad y restauración | ✅ | `scripts/backup.sh`, `restore.sh` |
| Retención y supresión | ✅ | Ley 21.719/RGPD documentado en `PRIVACIDAD.md` |

## 5.5 Pruebas
| Componente | Estado | Nota |
|---|---|---|
| Unitarias/API/telefonía/VAD/cascada/retención | ✅ | 116 verdes |
| Integración con Postgres/Redis reales | ✅ | Hallaron 2 defectos reales ya corregidos |
| Aislamiento multi-tenant a nivel de API | 🧪 | Cubierto para conversaciones; **falta** para cada recurso nuevo |
| Concurrencia / carga | 🟡 | `bench/loadtest.py`, sin umbral en CI |
| Seguridad (auditoría manual 2026-11-05) | 🟡 | Sin SAST/DAST automatizado más que `pip-audit` y Trivy en release |
| Cobertura | 🧪 | No se mide ni se exige en CI |
| Frontend (Playwright) | ⬜ | |

## 6–19 Módulos del documento maestro
| Módulo | Estado |
|---|---|
| Telephony Gateway (Twilio, webhooks, streaming) | 🟡 Twilio ✅; sin abstracción multi-proveedor, sin SIP, sin transferencia, sin idempotencia de eventos, estados de llamada parciales |
| Tool Execution Engine | ✅ validación/permiso/timeout/idempotencia. 🟡 faltan herramientas (modificar/cancelar reserva, clientes, confirmaciones, finalizar llamada) y confirmación explícita para acciones sensibles |
| Integraciones (Calendar, HubSpot…) | ⬜ |
| Base de conocimiento | 🟡 tabla con búsqueda full-text por organización; sin carga de PDF/DOCX/CSV, embeddings ni citas de fuente |
| Panel SaaS | 🟡 ver 5.1 |
| Multi-tenant | 🟡 aislamiento de datos ✅; alta de organizaciones, usuarios y roles ⬜ |
| Seguridad (MFA, rotación, cifrado de credenciales) | 🟡 JWT, RLS, CSP/HSTS, auditoría ✅; MFA, API keys, cifrado de secretos ⬜ |
| Analítica | 🟡 métricas técnicas ✅; comerciales (resolución, costo por llamada por agente) parciales |
| Facturación | ⬜ (solo `usage_records` y presupuesto diario) |
| API pública y webhooks | ⬜ |
| CI/CD | 🟡 CI, contrato y release ✅; sin despliegue a staging ni ambientes separados |

## Decisión sobre ruta telefónica (sección 7)
Ruta inicial: **proveedor con streaming de audio hacia el runtime propio (Twilio Media Streams)**, ya implementada y medida. SIP directo con OpenAI queda como segunda
ruta tras una interfaz `TelephonyProvider`; no se ha verificado su disponibilidad y condiciones, por lo que no se implementa todavía.

## Riesgos prioritarios
1. Sin gestión de agentes versionados no se puede publicar ni revertir con seguridad (bloquea el piloto).
2. Roles insuficientes para equipos reales.
3. Sin números telefónicos mapeados a organización/agente, la telefonía no es multi-tenant (hoy depende de configuración global).
4. Sin cobertura medida ni frontend probado, la regresión pasa desapercibida.
