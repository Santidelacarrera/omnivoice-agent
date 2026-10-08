"""Grabación de llamadas (opt-in con consentimiento) y almacenamiento externo.

* `StereoRecorder`: acumula audio del usuario (canal izquierdo) y del agente (derecho), alineados en el tiempo,
  en archivos temporales (no en RAM) y lo entrega como WAV PCM16 estéreo.
* `RecordingStorage`: interfaz con dos implementaciones: disco local (desarrollo) y S3 compatible (producción,
  cifrado en reposo con SSE). Las claves siguen `org/AAAA/MM/conversacion.wav`.

La grabación solo ocurre si lo permiten tres cosas a la vez: configuración global, política de la organización
y consentimiento explícito de la persona (ver `VoiceSession`).
"""
import array
import asyncio
import io
import os
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Protocol

import structlog

log = structlog.get_logger()

CHUNK_SAMPLES = 24000  # 1 s por bloque al mezclar


class StereoRecorder:
    """Dos pistas mono PCM16 alineadas por reloj. Izquierda = usuario, derecha = agente."""

    def __init__(self, sample_rate: int, max_seconds: int, clock=time.monotonic) -> None:
        self.rate = sample_rate
        self.max_samples = sample_rate * max_seconds
        self._clock = clock
        self._t0 = clock()
        self._files = {"user": tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024),
                       "agent": tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)}
        self._len = {"user": 0, "agent": 0}  # muestras escritas por pista
        self.truncated = False
        self.closed = False

    def _elapsed_samples(self) -> int:
        return int((self._clock() - self._t0) * self.rate)

    def _add(self, track: str, pcm: bytes) -> None:
        if self.closed or not pcm:
            return
        n = len(pcm) // 2
        if self._len[track] + n > self.max_samples:
            self.truncated = True
            return
        # Hueco hasta el instante de llegada → silencio. Si el audio llega más rápido que el tiempo real
        # (ráfagas del agente) se encola detrás, igual que la cola de reproducción del navegador.
        gap = self._elapsed_samples() - n - self._len[track]
        f = self._files[track]
        if gap > 0 and self._len[track] + gap + n <= self.max_samples:
            f.write(b"\x00\x00" * gap)
            self._len[track] += gap
        f.write(pcm[: n * 2])
        self._len[track] += n

    def add_user(self, pcm: bytes) -> None:
        self._add("user", pcm)

    def add_agent(self, pcm: bytes) -> None:
        self._add("agent", pcm)

    def drop_pending_agent_audio(self) -> None:
        """Barge-in: el audio del agente que aún no sonó no debe quedar grabado."""
        now = self._elapsed_samples()
        if self._len["agent"] > now:
            f = self._files["agent"]
            f.truncate(now * 2)
            f.seek(0, os.SEEK_END)
            self._len["agent"] = now

    @property
    def seconds(self) -> float:
        return max(self._len.values()) / self.rate

    def discard(self) -> None:
        """Revocación del consentimiento: se borra todo lo acumulado."""
        self.closed = True
        for f in self._files.values():
            f.close()

    def to_wav(self) -> bytes:
        """WAV estéreo PCM16. Se llama una vez, al cerrar la sesión."""
        self.closed = True
        total = max(self._len.values())
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            for f in self._files.values():
                f.seek(0)
            pos = 0
            while pos < total:
                n = min(CHUNK_SAMPLES, total - pos)
                left = array.array("h")
                right = array.array("h")
                left.frombytes(self._files["user"].read(n * 2))
                right.frombytes(self._files["agent"].read(n * 2))
                for a in (left, right):
                    if len(a) < n:
                        a.extend(array.array("h", bytes(2 * (n - len(a)))))
                mixed = array.array("h", bytes(4 * n))
                mixed[0::2] = left
                mixed[1::2] = right
                w.writeframes(mixed.tobytes())
                pos += n
        for f in self._files.values():
            f.close()
        return out.getvalue()


def recording_key(org_id: str, conversation_id: str, now: float | None = None) -> str:
    t = time.gmtime(now if now is not None else time.time())
    return f"{org_id}/{t.tm_year:04d}/{t.tm_mon:02d}/{conversation_id}.wav"


class RecordingStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = "audio/wav") -> None: ...
    async def get(self, key: str) -> bytes | None: ...
    async def delete(self, key: str) -> None: ...
    async def presigned_url(self, key: str, ttl: int) -> str | None: ...


class LocalRecordingStorage:
    """Disco local. Solo para desarrollo: en producción usa S3 (u otro almacenamiento de objetos)."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("clave de grabación fuera del directorio")
        return p

    async def put(self, key, data, content_type="audio/wav"):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    async def get(self, key):
        p = self._path(key)
        return await asyncio.to_thread(p.read_bytes) if p.exists() else None

    async def delete(self, key):
        p = self._path(key)
        if p.exists():
            await asyncio.to_thread(p.unlink)

    async def presigned_url(self, key, ttl):
        return None  # el API sirve el archivo directamente


class S3RecordingStorage:
    """S3 o compatible (MinIO, R2). Cifrado en reposo (SSE) y URLs prefirmadas de vida corta."""

    def __init__(self, bucket: str, region: str | None = None, endpoint_url: str | None = None,
                 sse: str = "AES256", kms_key_id: str | None = None, client: Any = None) -> None:
        if client is None:
            import boto3  # import perezoso: solo hace falta con RECORDING_STORAGE=s3

            client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)
        self.c = client
        self.bucket = bucket
        self.sse = sse
        self.kms_key_id = kms_key_id

    async def put(self, key, data, content_type="audio/wav"):
        extra: dict[str, Any] = {"ServerSideEncryption": self.sse}
        if self.sse == "aws:kms" and self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id
        await asyncio.to_thread(self.c.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **extra)

    async def get(self, key):
        def _get():
            try:
                return self.c.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    return None
                raise

        return await asyncio.to_thread(_get)

    async def delete(self, key):
        await asyncio.to_thread(self.c.delete_object, Bucket=self.bucket, Key=key)  # idempotente en S3

    async def presigned_url(self, key, ttl):
        return await asyncio.to_thread(
            self.c.generate_presigned_url, "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl)


def build_storage(settings: Any) -> RecordingStorage | None:
    """None = grabación desactivada globalmente (ninguna sesión se graba)."""
    kind = settings.recording_storage
    if kind == "none":
        return None
    if kind == "local":
        return LocalRecordingStorage(settings.recording_local_dir)
    if kind == "s3":
        if not settings.recording_s3_bucket:
            raise ValueError("RECORDING_S3_BUCKET es obligatorio con RECORDING_STORAGE=s3")
        return S3RecordingStorage(settings.recording_s3_bucket, settings.recording_s3_region or None,
                                  settings.recording_s3_endpoint_url or None, settings.recording_s3_sse,
                                  settings.recording_s3_kms_key_id or None)
    raise ValueError(f"RECORDING_STORAGE desconocido: {kind}")
