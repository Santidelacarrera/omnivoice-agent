"""Marco de integraciones: conectores HTTP genéricos que la organización define y el agente usa mediante dos herramientas
(`integration_read` solo GET; `integration_write` POST/PUT/PATCH/DELETE y exige confirmación del usuario).

Qué garantiza (probado con servidores simulados):
  * el destino es el configurado: HTTPS, red pública, IP validada y fijada, sin redirecciones (misma protección SSRF que los webhooks);
  * el agente solo elige método, ruta y parámetros, y solo dentro de `allowed_methods` / `allowed_paths` (sin `..`, sin cambiar de host);
  * las credenciales (bearer, cabecera, basic, OAuth2 client credentials con renovación del token) van cifradas y nunca se devuelven ni se registran;
  * la respuesta se acota (64 KB) y se entrega al modelo como **datos**, no como instrucciones.

Qué NO está verificado: ninguna API real de terceros (Google, Outlook, HubSpot, Salesforce...). Funciona con APIs REST con JSON y
autenticación estándar; Google/Outlook/HubSpot por OAuth con usuario (código de autorización) NO están soportados: requieren registrar
una aplicación OAuth y un flujo de consentimiento que esta capa no implementa.
"""
from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote, urlencode

import httpx
import structlog

from app.core.config import Settings
from app.security import netguard, secretbox

log = structlog.get_logger()

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
AUTH_TYPES = ("none", "bearer", "header", "basic", "oauth2_client_credentials")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,60}$")
MAX_RESPONSE_BYTES = 64 * 1024
MAX_BODY_BYTES = 8 * 1024
MAX_INTEGRATIONS = 20
TIMEOUT_S = 8.0
RESULT_CHARS = 4000  # lo que se entrega al modelo


class IntegrationError(Exception):
    """Error de uso (configuración o llamada no permitida). `code` es estable; el mensaje es apto para mostrar."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def clean_path(path: str) -> str:
    """Valida una ruta relativa al conector: empieza por `/`, sin `..`, `//`, `\\`, `?`, `#` ni caracteres de control (ni codificados)."""
    if not path.startswith("/") or path.startswith("//") or len(path) > 500:
        raise IntegrationError("invalid_path", "La ruta debe empezar por / y tener menos de 500 caracteres")
    for candidate in (path, unquote(path), unquote(unquote(path))):
        if ".." in candidate.split("/") or any(ch in candidate for ch in ("\\", "?", "#", "\x00", "\r", "\n", "\t")):
            raise IntegrationError("invalid_path", "Ruta no permitida")
    return path


def path_allowed(path: str, prefixes: list[str]) -> bool:
    for p in prefixes:
        base = p.rstrip("/")
        if p == "/" or path == base or path.startswith(base + "/"):
            return True
    return False


def validate_config(body: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Normaliza y valida la configuración recibida de la API; lanza IntegrationError con un mensaje claro."""
    out: dict[str, Any] = {}
    if "name" in body:
        if not NAME_RE.match(str(body["name"])):
            raise IntegrationError("invalid_name", "El nombre: 2-40 caracteres en minúscula, números, guion o guion bajo")
        out["name"] = body["name"]
    elif not partial:
        raise IntegrationError("invalid_name", "Falta el nombre")
    if "methods" in body:
        methods = [m.upper() for m in body["methods"]]
        if not methods or any(m not in METHODS for m in methods):
            raise IntegrationError("invalid_methods", f"Métodos permitidos: {', '.join(METHODS)}")
        out["allowed_methods"] = sorted(set(methods))
    if "paths" in body:
        if not body["paths"] or len(body["paths"]) > 20:
            raise IntegrationError("invalid_paths", "Indica entre 1 y 20 rutas permitidas")
        out["allowed_paths"] = [("/" if p == "/" else clean_path(p).rstrip("/")) for p in body["paths"]]
    for key in ("base_url", "token_url"):
        if body.get(key):
            scheme, host, _, path = netguard.check_syntax(body[key])  # https obligatorio; sin credenciales en la URL
            out[key] = body[key]
    if not partial and "base_url" not in out:
        raise IntegrationError("invalid_url", "Falta la URL base")
    if "auth_type" in body:
        if body["auth_type"] not in AUTH_TYPES:
            raise IntegrationError("invalid_auth", f"auth_type: {', '.join(AUTH_TYPES)}")
        out["auth_type"] = body["auth_type"]
    for key in ("auth_header", "scope"):
        if key in body:
            out[key] = body[key] or None
    if out.get("auth_header") and not HEADER_RE.match(out["auth_header"]):
        raise IntegrationError("invalid_auth", "auth_header: solo letras, números y guion (máx. 60)")
    if out.get("scope") and (len(out["scope"]) > 200 or any(ord(ch) < 32 for ch in out["scope"])):
        raise IntegrationError("invalid_auth", "scope inválido")
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    return out


def build_credential(auth_type: str, cred: dict[str, Any] | None, auth_header: str | None, token_url: str | None) -> str | None:
    """Devuelve el JSON de credenciales a cifrar (o None si el tipo no lleva)."""
    cred = cred or {}
    needs = {"bearer": ("token",), "header": ("value",), "basic": ("username", "password"),
             "oauth2_client_credentials": ("client_id", "client_secret")}.get(auth_type, ())
    if auth_type == "none":
        return None
    missing = [k for k in needs if not cred.get(k)]
    if missing:
        raise IntegrationError("invalid_credential", f"Faltan credenciales: {', '.join(missing)}")
    if any(len(str(cred[k])) > 2000 or any(ord(ch) < 32 or ord(ch) == 127 for ch in str(cred[k])) for k in needs):
        raise IntegrationError("invalid_credential", "Las credenciales no pueden llevar saltos de línea ni caracteres de control (máx. 2000)")
    if auth_type == "header" and not auth_header:
        raise IntegrationError("invalid_auth", "auth_type header exige auth_header (nombre de la cabecera)")
    if auth_type == "oauth2_client_credentials" and not token_url:
        raise IntegrationError("invalid_auth", "OAuth2 exige token_url")
    return json.dumps({k: str(cred[k]) for k in needs})


class IntegrationService:
    def __init__(self, s: Settings, db_getter: Callable[[], Any], http: httpx.AsyncClient | None = None,
                 resolver: netguard.Resolver = netguard.system_resolver) -> None:
        self.s, self._db, self.resolver = s, db_getter, resolver
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S), follow_redirects=False)
        self._tokens: dict[str, tuple[str, float]] = {}  # (org, integración) -> (token, caduca): solo en memoria de este proceso

    @property
    def db(self):
        return self._db()

    @property
    def key_material(self) -> str:
        return self.s.webhook_secret_key or self.s.jwt_secret

    def encrypt(self, plaintext: str | None) -> str | None:
        return secretbox.encrypt(self.key_material, plaintext) if plaintext else None

    def _allow_private(self) -> bool:
        return self.s.webhooks_allow_private  # solo pruebas locales; en producción exige HTTPS y red pública

    async def _send(self, method: str, url: str, *, headers: dict[str, str], content: bytes | None = None,
                    params: dict[str, str] | None = None) -> tuple[int, bytes, str, bool]:
        """Una petición con destino validado: conecta a la IP resuelta (anti DNS rebinding), sin redirecciones y con tope de tamaño."""
        try:
            scheme, host, port, path, ip = await netguard.resolve_safe(url, self.resolver, allow_private=self._allow_private(),
                                                                      allow_http=self._allow_private())
        except netguard.UnsafeUrl as exc:
            raise IntegrationError("destination_not_allowed", str(exc)) from None
        default_port = 443 if scheme == "https" else 80
        host_header = host if port == default_port else f"{host}:{port}"
        target = f"{scheme}://[{ip}]:{port}{path}" if ":" in ip else f"{scheme}://{ip}:{port}{path}"
        h = {**headers, "Host": host_header, "User-Agent": "OmniVoice-Integrations/1"}
        try:
            async with self.http.stream(method, target, headers=h, content=content, params=params,
                                        extensions={"sni_hostname": host}, follow_redirects=False) as r:
                buf, truncated = bytearray(), False
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_RESPONSE_BYTES:
                        buf, truncated = buf[:MAX_RESPONSE_BYTES], True
                        break
                return r.status_code, bytes(buf), r.headers.get("content-type", ""), truncated
        except httpx.TimeoutException:
            raise IntegrationError("timeout", "El servicio externo no respondió a tiempo") from None
        except httpx.HTTPError as exc:
            raise IntegrationError("connection_error", f"No se pudo conectar ({type(exc).__name__})") from None

    async def _oauth_token(self, integ: dict[str, Any], cred: dict[str, str]) -> str:
        key = f"{integ['org_id']}:{integ['id']}"
        hit = self._tokens.get(key)
        if hit and hit[1] > time.monotonic() + 30:
            return hit[0]
        form = {"grant_type": "client_credentials", "client_id": cred["client_id"], "client_secret": cred["client_secret"]}
        if integ.get("scope"):
            form["scope"] = integ["scope"]
        status, body, _, _ = await self._send("POST", integ["token_url"], headers={"Content-Type": "application/x-www-form-urlencoded",
                                                                                   "Accept": "application/json"}, content=urlencode(form).encode())
        try:
            data = json.loads(body)
            token = data["access_token"]
        except (ValueError, KeyError, TypeError):
            raise IntegrationError("auth_failed", f"El servicio de autenticación no entregó un token (HTTP {status})") from None
        if not (200 <= status < 300) or not isinstance(token, str):
            raise IntegrationError("auth_failed", f"No se pudo obtener el token (HTTP {status})")
        ttl = data.get("expires_in")
        self._tokens[key] = (token, time.monotonic() + (float(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else 300.0))
        return token

    async def _auth_headers(self, integ: dict[str, Any]) -> dict[str, str]:
        kind = integ["auth_type"]
        if kind == "none":
            return {}
        try:
            cred = json.loads(secretbox.decrypt(self.key_material, integ["credential_enc"] or ""))
        except (secretbox.SecretBoxError, ValueError):
            raise IntegrationError("credential_unreadable", "No se pudo leer la credencial guardada; vuelve a configurarla") from None
        if kind == "bearer":
            return {"Authorization": f"Bearer {cred['token']}"}
        if kind == "header":
            return {integ["auth_header"]: cred["value"]}
        if kind == "basic":
            return {"Authorization": "Basic " + base64.b64encode(f"{cred['username']}:{cred['password']}".encode()).decode()}
        return {"Authorization": f"Bearer {await self._oauth_token(integ, cred)}"}

    def invalidate_token(self, org_id: str, integration_id: str) -> None:
        self._tokens.pop(f"{org_id}:{integration_id}", None)

    async def call(self, org_id: str, name: str, method: str, path: str, query: dict[str, str] | None = None,
                   body: dict[str, Any] | None = None, actor: str | None = None) -> dict[str, Any]:
        """Ejecuta una llamada permitida y devuelve `{status, ok, data|text, truncated}`. Lanza IntegrationError si no está permitida."""
        method = method.upper()
        integ = await self.db.integration_get(org_id, name)
        if not integ or not integ["enabled"]:
            raise IntegrationError("not_found", f"No existe la integración «{name}» o está desactivada")
        if method not in integ["allowed_methods"]:
            raise IntegrationError("method_not_allowed", f"La integración «{name}» no permite {method}")
        path = clean_path(path)
        if not path_allowed(path, integ["allowed_paths"]):
            raise IntegrationError("path_not_allowed", f"La ruta no está permitida en «{name}»")
        if query and (len(query) > 10 or any(len(str(k)) > 60 or len(str(v)) > 300 for k, v in query.items())):
            raise IntegrationError("invalid_query", "Demasiados parámetros o demasiado largos")
        content, headers = None, {"Accept": "application/json"}
        if body is not None:
            content = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
            if len(content) > MAX_BODY_BYTES:
                raise IntegrationError("body_too_large", "El cuerpo supera 8 KB")
            headers["Content-Type"] = "application/json"
        base = integ["base_url"].rstrip("/")
        url = base + quote(path, safe="/%:@!$&'()*+,;=-._~")
        status, raw, ctype, truncated = None, b"", "", False
        for attempt in (0, 1):  # un 401 con OAuth2 puede ser un token revocado: se renueva una vez
            auth = await self._auth_headers(integ)
            status, raw, ctype, truncated = await self._send(method, url, headers={**headers, **auth}, content=content,
                                                             params={str(k): str(v) for k, v in (query or {}).items()} or None)
            if status == 401 and attempt == 0 and integ["auth_type"] == "oauth2_client_credentials":
                self.invalidate_token(org_id, integ["id"])
                continue
            break
        await self.db.audit(org_id, actor, "integration.call", {"integration": name, "method": method, "path": path, "status": status})
        out: dict[str, Any] = {"status": status, "ok": 200 <= status < 300, "truncated": truncated}
        text = raw.decode("utf-8", errors="replace")
        if "json" in ctype.lower() and not truncated:
            try:
                out["data"] = json.loads(text)
                return out
            except ValueError:
                pass
        out["text"] = text[:RESULT_CHARS]
        return out
