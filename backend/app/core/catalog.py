"""Idiomas y voces que ofrece la plataforma (nombres para la UI y para las instrucciones del agente)."""

LANGUAGE_NAMES: dict[str, str] = {
    "es": "español", "en": "English", "pt": "português", "fr": "français", "de": "Deutsch", "it": "italiano",
}


def language_name(code: str | None) -> str | None:
    return LANGUAGE_NAMES.get((code or "").split("-")[0])


def language_directive(code: str | None) -> str:
    name = language_name(code)
    return f"\nResponde siempre en {name} y entiende al usuario en ese idioma." if name else ""
