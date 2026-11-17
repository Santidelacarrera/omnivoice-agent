"""Protección contra SSRF para destinos que el cliente controla (webhooks).

Una URL de webhook la elige un usuario de la organización; sin control podría apuntar a servicios internos (metadatos de la nube,
Redis, PostgreSQL, el propio backend). Reglas: solo https, sin credenciales en la URL, y el nombre debe resolver **solo** a direcciones
públicas. Se comprueba al registrar y otra vez antes de cada envío (el DNS puede cambiar), y el envío se fija a la IP validada
(ver `services/webhooks.py`) para que no se pueda resolver a otra distinta entre la comprobación y la conexión.
"""
import asyncio
import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

Resolver = Callable[[str], list[str]]


class UnsafeUrl(ValueError):
    """La URL no es un destino permitido (mensaje seguro de mostrar)."""


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeUrl("No se pudo resolver el nombre del servidor") from None
    return sorted({i[4][0] for i in infos})


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 es la red privada 10.0.0.1
    return ip.is_global and not ip.is_multicast


def check_syntax(url: str, allow_http: bool = False) -> tuple[str, str, int, str]:
    """Valida la forma de la URL. Devuelve (esquema, host, puerto, ruta+consulta)."""
    if len(url) > 2000:
        raise UnsafeUrl("La URL es demasiado larga")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeUrl("URL inválida") from None
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise UnsafeUrl("La URL debe usar https")
    if not parts.hostname or parts.username or parts.password:
        raise UnsafeUrl("La URL no puede llevar credenciales y debe tener un servidor")
    if parts.fragment:
        raise UnsafeUrl("La URL no puede llevar fragmento")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.scheme, parts.hostname, port or (443 if parts.scheme == "https" else 80), path


async def resolve_safe(url: str, resolver: Resolver = system_resolver, allow_private: bool = False,
                       allow_http: bool = False) -> tuple[str, str, int, str, str]:
    """Comprueba la URL y devuelve (esquema, host, puerto, ruta, ip_validada). Falla si alguna dirección no es pública."""
    scheme, host, port, path = check_syntax(url, allow_http=allow_http)
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        addrs = await asyncio.get_running_loop().run_in_executor(None, resolver, host)
    if not addrs:
        raise UnsafeUrl("No se pudo resolver el nombre del servidor")
    if not allow_private and not all(is_public(a) for a in addrs):
        raise UnsafeUrl("La URL apunta a una red privada o no permitida")
    return scheme, host, port, path, addrs[0]
