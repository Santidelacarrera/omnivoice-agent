"""Expansión de consultas para la búsqueda de conocimiento, sin modelos ni servicios externos.

El texto completo de PostgreSQL ya aplica el stemmer español, pero no enlaza variantes de distinta raíz («devolver» / «devoluciones»)
ni sinónimos («reembolso»). Aquí se añaden dos cosas, ambas deterministas y auditables:
  1. coincidencia por prefijo de las primeras letras de las palabras largas (misma familia léxica);
  2. grupos de sinónimos: unos de serie para atención al cliente y los que configure la empresa (`KB_SYNONYMS_JSON`).
No es búsqueda semántica: lo que no comparte prefijo ni está en un grupo sigue sin encontrarse (para eso hacen falta embeddings).
"""
from __future__ import annotations

import re
import unicodedata

DEFAULT_SYNONYMS: list[list[str]] = [
    ["devolver", "devolucion", "reembolso", "reintegro", "reembolsar", "cambio"],
    ["horario", "hora", "abierto", "abren", "cierran", "atencion"],
    ["envio", "entrega", "despacho", "enviar", "reparto", "domicilio"],
    ["precio", "costo", "coste", "tarifa", "valor", "cuesta", "cuanto"],
    ["pago", "pagar", "cobro", "tarjeta", "transferencia", "efectivo"],
    ["garantia", "defecto", "falla", "averia", "reparacion"],
    ["cancelar", "anular", "cancelacion", "anulacion"],
    ["reserva", "reservar", "cita", "turno"],
    ["telefono", "llamar", "contacto", "contactar"],
]
STOPWORDS = {"que", "como", "cual", "cuales", "cuando", "donde", "para", "por", "con", "sin", "los", "las", "una", "uno", "del", "este",
             "esta", "mis", "puedo", "quiero", "tengo", "hay", "son", "muy", "mas", "pero", "sus", "sobre", "desde", "hasta"}
MAX_TERMS = 12
PREFIX_LEN = 5

_groups: list[set[str]] = []


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize(text))


def configure(extra_groups: list[list[str]] | None = None) -> None:
    """Fija los grupos activos: los de serie más los de la empresa (cada grupo con al menos dos palabras)."""
    global _groups
    merged = DEFAULT_SYNONYMS + [g for g in (extra_groups or []) if isinstance(g, list) and len(g) >= 2]
    _groups = [{w for item in g for w in words(str(item)) if len(w) >= 3} for g in merged]


def stem_prefix(word: str) -> str | None:
    """Prefijo para coincidencia por familia léxica; None si la palabra es corta y debe coincidir entera."""
    return word[:PREFIX_LEN] if len(word) >= 7 else None


def expand(query: str) -> list[str]:
    """Términos de búsqueda en forma `palabra` o `prefijo*` (con `*` al final cuando es un prefijo). Orden estable, sin duplicados."""
    if not _groups:
        configure()
    out: dict[str, None] = {}
    for w in words(query):
        if len(w) < 3 or w in STOPWORDS:
            continue
        group_words = {w}
        for g in _groups:
            if any(w == x or (len(x) >= 5 and w.startswith(x[:PREFIX_LEN]) and len(w) >= 5) for x in g):
                group_words |= g
        for gw in sorted(group_words, key=lambda x: (x != w, x)):
            p = stem_prefix(gw)
            out[f"{p}*" if p else gw] = None
        if len(out) >= MAX_TERMS:
            break
    return list(out)[:MAX_TERMS]


def matches(term: str, doc_words: set[str]) -> bool:
    """Equivalente en memoria de un término de `expand`."""
    if term.endswith("*"):
        return any(d.startswith(term[:-1]) for d in doc_words)
    return term in doc_words
