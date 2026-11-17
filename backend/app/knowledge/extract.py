"""Extracción de texto y fragmentación de documentos de la base de conocimiento.

Los archivos los sube un usuario, así que se tratan como **no confiables**:
- El tipo se decide por el contenido (firma del archivo), no por el nombre ni por lo que declare el cliente.
- Límites duros de tamaño, de descompresión (bomba zip en DOCX), de páginas (PDF) y de texto total.
- DOCX se lee como zip y solo `word/document.xml`, analizado con `defusedxml` y **sin permitir DTD**: bloquea entidades externas (XXE) y la expansión de entidades («billion laughs»).
- Nada se ejecuta ni se interpreta: solo se extrae texto. El texto resultante es *dato*, no instrucciones (el agente lo recibe marcado así).
"""
import csv
import io
import re
import zipfile
from xml.etree.ElementTree import ParseError  # noqa: S405 - solo la excepción; el análisis lo hace defusedxml

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

MAX_BYTES = 5 * 1024 * 1024  # archivo subido
MAX_UNCOMPRESSED = 25 * 1024 * 1024  # DOCX descomprimido
MAX_PDF_PAGES = 200
MAX_TEXT_CHARS = 1_500_000
CHUNK_CHARS = 700
CHUNK_OVERLAP = 100
KINDS = ("txt", "csv", "docx", "pdf")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"[ \t\f\v]+")
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ExtractionError(ValueError):
    """El archivo no se puede aceptar (mensaje seguro de mostrar)."""


def detect_kind(data: bytes) -> str:
    """Tipo por contenido: PDF (%PDF), DOCX (zip con word/document.xml) o texto. CSV se declara aparte porque es texto."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            raise ExtractionError("El archivo está dañado") from None
        raise ExtractionError("Solo se aceptan documentos DOCX, PDF, TXT y CSV")
    return "txt"


def clean(text: str) -> str:
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ExtractionError("No se pudo leer el texto del archivo")


def _txt(data: bytes) -> str:
    text = _decode(data)
    if text and sum(1 for c in text[:20000] if _CONTROL.match(c)) / min(len(text), 20000) > 0.02:
        raise ExtractionError("El archivo no parece texto (¿es un binario?)")
    return text


def _csv(data: bytes) -> str:
    text = _decode(data)
    try:
        rows = list(csv.reader(io.StringIO(text), dialect=csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")))
    except csv.Error:
        rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ""
    header = [h.strip() for h in rows[0]]
    out = []
    for row in rows[1:] or []:
        cells = [f"{header[i] if i < len(header) and header[i] else f'columna {i + 1}'}: {v.strip()}" for i, v in enumerate(row) if v.strip()]
        if cells:
            out.append(". ".join(cells) + ".")
    return "\n".join(out) if len(rows) > 1 else "\n".join(header)


def _docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_UNCOMPRESSED or sum(i.file_size for i in z.infolist()) > MAX_UNCOMPRESSED:
                raise ExtractionError("El documento descomprimido es demasiado grande")
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise ExtractionError("El archivo DOCX está dañado") from None
    try:
        root = SafeET.fromstring(xml, forbid_dtd=True)
    except (ParseError, DefusedXmlException):
        raise ExtractionError("El archivo DOCX está dañado o contiene construcciones no permitidas") from None
    paragraphs = []
    for p in root.iter(f"{W_NS}p"):
        text = "".join((t.text or "") for t in p.iter(f"{W_NS}t"))
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError:  # pragma: no cover
        raise ExtractionError("La lectura de PDF no está disponible en este servidor") from None
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ExtractionError("El PDF está protegido con contraseña")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ExtractionError(f"El PDF tiene más de {MAX_PDF_PAGES} páginas")
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except ExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, RecursionError, OSError):
        raise ExtractionError("No se pudo leer el PDF (¿está dañado o es solo imágenes?)") from None


def extract(data: bytes, declared: str | None = None) -> tuple[str, str]:
    """Devuelve (texto limpio, tipo real). `declared` solo distingue CSV de TXT (ambos son texto)."""
    if not data:
        raise ExtractionError("El archivo está vacío")
    if len(data) > MAX_BYTES:
        raise ExtractionError(f"El archivo supera {MAX_BYTES // (1024 * 1024)} MB")
    kind = detect_kind(data)
    if kind == "txt" and declared == "csv":
        kind = "csv"
    if declared and declared in KINDS and declared != kind and not (declared == "csv" and kind == "csv"):
        raise ExtractionError(f"El contenido es {kind.upper()}, no {declared.upper()}")
    text = clean({"txt": _txt, "csv": _csv, "docx": _docx, "pdf": _pdf}[kind](data))
    if not text:
        raise ExtractionError("No se encontró texto en el archivo")
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractionError("El texto del documento es demasiado largo")
    return text, kind


def chunk(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Fragmentos de ~`size` caracteres cortados en límites de frase o párrafo, con solape para no perder el contexto."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}|\n", text) if p.strip()]
    units: list[str] = []
    for p in paragraphs:  # un párrafo largo se parte por frases
        if len(p) <= size:
            units.append(p)
        else:
            units.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", p) if s.strip())
    out: list[str] = []
    cur = ""
    for u in units:
        while len(u) > size:  # frase gigante: corte duro
            if cur:
                out.append(cur)
                cur = ""
            out.append(u[:size])
            u = u[size - overlap:]
        if cur and len(cur) + 1 + len(u) > size:
            out.append(cur)
            cur = cur[-overlap:].split(" ", 1)[-1] + " " + u if overlap and len(cur) > overlap else u
        else:
            cur = f"{cur}\n{u}".strip() if cur else u
    if cur:
        out.append(cur)
    return out
