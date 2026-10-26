"""tts-server: FastAPI front end for the GPU scheduler.

OpenAI-compatible subset: POST /v1/audio/speech with `model`, `input`, `voice`, `response_format`
(wav | flac | mp3). Extensions (not in OpenAI's API): `language` (code or name, e.g. "zh" /
"Chinese") and `seed` (int, 0..2**32-1). Any other field is rejected with `unsupported_parameter`.
POST /v1/jobs and /v1/audio/speech accept an `Idempotency-Key` header: retrying a submission with the
same key returns the original job instead of generating again.
"""
import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from local_tts.audio import FORMATS
from local_tts.config import load_registry, load_server_config
from local_tts.engine import ModelManager, SynthesisRequest
from local_tts.errors import TTSError
from local_tts.text import pause_values
from local_tts.server.scheduler import Job, Scheduler

log = logging.getLogger("local_tts")

MAX_INPUT_CHARS = 20000
MAX_PAUSE_SECONDS = 60.0
MAX_TOTAL_PAUSE_SECONDS = 600.0
# Request bodies are refused before parsing above this size (20,000 chars of JSON-escaped text fit).
MAX_BODY_BYTES = 256 * 1024
# Per-segment seeds are derived by adding small offsets, so keep the base far from torch's 64-bit limit.
MAX_SEED = 2**32 - 1


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str
    model: Optional[str] = None
    voice: Optional[str] = None
    response_format: str = "wav"
    # Extensions beyond the OpenAI API.
    language: Optional[str] = None
    seed: Optional[int] = Field(default=None, ge=0, le=MAX_SEED)


EXTENSION_FIELDS = ("language", "seed")


def build_app() -> FastAPI:
    cfg = load_server_config()
    registry = load_registry()
    manager = ModelManager(registry)
    scheduler = Scheduler(manager, cfg)
    started = time.time()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler.start()
        log.info("tts-server ready on %s:%d (models: %s)", cfg.host, cfg.port, ", ".join(registry.models))
        yield
        scheduler.stop()

    app = FastAPI(title="local-tts", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY_BYTES)

    @app.exception_handler(TTSError)
    async def tts_error(_: Request, e: TTSError):
        headers = {"Retry-After": "10"} if e.code in ("queue_full", "results_full") else None
        return JSONResponse({"error": e.to_dict()}, status_code=e.http_status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError):
        extra = [str(err["loc"][-1]) for err in e.errors() if err["type"] == "extra_forbidden"]
        if extra:
            err = TTSError("unsupported_parameter", f"Unsupported parameter(s): {', '.join(extra)}.", 400,
                           parameters=extra, supported=sorted(SpeechRequest.model_fields))
        else:
            err = TTSError("invalid_request", "Invalid request body.", 400,
                           errors=[{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()])
        return JSONResponse({"error": err.to_dict()}, status_code=err.http_status)

    def model_info(model_id: str) -> dict:
        entry = registry.models[model_id]
        adapter = manager.adapter(model_id)
        return {
            "id": model_id, "object": "model", "owned_by": "local",
            "description": entry.description, "default": model_id == registry.default_model,
            "loaded": manager.loaded_model == model_id,
            "languages": sorted(adapter.languages), "capabilities": adapter.capabilities,
            "options": sorted(adapter.supported_options),
        }

    def to_request(body: SpeechRequest) -> SynthesisRequest:
        entry = registry.resolve_model(body.model)
        voice = registry.resolve_voice(body.voice, entry.id)
        adapter = manager.adapter(entry.id)
        given = [f for f in EXTENSION_FIELDS if getattr(body, f) is not None]
        unsupported = [f for f in given if f not in adapter.supported_options]
        if unsupported:
            raise TTSError("unsupported_parameter",
                           f"Model '{entry.id}' does not support: {', '.join(unsupported)}.", 400,
                           parameters=unsupported, supported=sorted(adapter.supported_options))
        if body.response_format not in FORMATS:
            raise TTSError("unsupported_format", f"Unsupported response_format '{body.response_format}'.", 400,
                           available=sorted(FORMATS))
        if not body.input.strip():
            raise TTSError("empty_input", "Input is empty.", 400)
        if len(body.input) > MAX_INPUT_CHARS:
            raise TTSError("input_too_long", f"Input exceeds {MAX_INPUT_CHARS} characters.", 400,
                           max_chars=MAX_INPUT_CHARS)
        pauses = pause_values(body.input)
        if any(not math.isfinite(p) or p > MAX_PAUSE_SECONDS for p in pauses) or sum(pauses) > MAX_TOTAL_PAUSE_SECONDS:
            raise TTSError("invalid_pause", f"[pause] markers must each be <= {MAX_PAUSE_SECONDS:.0f}s and "
                           f"total <= {MAX_TOTAL_PAUSE_SECONDS:.0f}s.", 400,
                           max_pause_seconds=MAX_PAUSE_SECONDS, max_total_pause_seconds=MAX_TOTAL_PAUSE_SECONDS)
        language = adapter.normalize_language(body.language)
        return SynthesisRequest(model=entry, voice=voice, text=body.input, response_format=body.response_format,
                                language=language, seed=body.seed)

    def audio_response(job: Job) -> Response:
        status, error, http_status, audio, m = scheduler.outcome(job)
        if error is not None:
            details = {k: v for k, v in error.items() if k not in ("code", "message")}
            raise TTSError(error["code"], error["message"], http_status, job_id=job.id, **details)
        if status != "succeeded":
            raise TTSError("job_not_finished", f"Job '{job.id}' is {status}.", 409, job_status=status)
        headers = {  # names may be non-ASCII (registry keys), so they are percent-encoded
            "X-TTS-Job-Id": job.id, "X-TTS-Model": quote(m["model"]), "X-TTS-Voice": quote(m["voice"] or ""),
            "X-TTS-Duration": str(m["duration"]), "X-TTS-Sample-Rate": str(m["sample_rate"]),
            "X-TTS-Channels": str(m["channels"]), "X-TTS-Segments": str(m["segments"]),
            "X-TTS-Language": quote(m["language"] or ""), "X-TTS-Retries": str(m["retries"]),
            "X-TTS-Splits": str(m["splits"]),
        }
        return Response(audio, media_type=m["content_type"], headers=headers)

    @app.get("/health")
    def health():
        return {
            "status": "ok", "uptime_seconds": round(time.time() - started, 1),
            "loaded_model": manager.loaded_model, "default_model": registry.default_model,
            **scheduler.stats(), "queue_max": cfg.queue_max,
            "idle_unload_seconds": cfg.idle_unload_seconds,
            "gpu": _gpu_memory(),
        }

    @app.get("/v1/models")
    def list_models():
        return {"object": "list", "data": [model_info(m) for m in registry.models]}

    @app.get("/v1/voices")
    def list_voices(model: Optional[str] = None):
        if model is not None:
            registry.resolve_model(model)
        return {"object": "list", "data": [
            {"name": v.name, "description": v.description, "language": v.language, "models": v.models}
            for v in registry.voices.values() if model is None or model in v.models
        ]}

    def idempotency_key(value: Optional[str]) -> Optional[str]:
        if value is not None and not (0 < len(value) <= 128 and value.isascii() and value.isprintable()):
            raise TTSError("invalid_request", "Idempotency-Key must be 1-128 printable ASCII characters.", 400)
        return value

    @app.post("/v1/audio/speech")
    async def speech(body: SpeechRequest, idempotency_key_: Optional[str] = Header(None, alias="Idempotency-Key")):
        job, _ = scheduler.submit(to_request(body), idempotency_key(idempotency_key_))
        if not await asyncio.to_thread(job.done.wait, cfg.sync_timeout_seconds):
            # The job keeps running; the client can poll /v1/jobs/{id} instead of resubmitting.
            raise TTSError("sync_timeout", f"Not finished within {cfg.sync_timeout_seconds:.0f}s; "
                           f"poll /v1/jobs/{job.id}.", 504, job_id=job.id)
        return audio_response(job)

    @app.post("/v1/jobs", status_code=202)
    def create_job(body: SpeechRequest, idempotency_key_: Optional[str] = Header(None, alias="Idempotency-Key")):
        # A repeated Idempotency-Key returns the job created by the first request (no new generation).
        job, _ = scheduler.submit(to_request(body), idempotency_key(idempotency_key_))
        return scheduler.snapshot(job)

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str):
        return scheduler.snapshot(scheduler.get(job_id))

    @app.get("/v1/jobs/{job_id}/audio")
    def get_job_audio(job_id: str):
        return audio_response(scheduler.get(job_id))

    return app


class BodyLimit:
    """ASGI middleware: 413 for bodies over `max_bytes`. The body is read here (at most `max_bytes`,
    checked on Content-Length and while streaming) and replayed to the app, so an oversized request
    is never read into memory or parsed."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        too_large = length is not None and (not length.isdigit() or int(length) > self.max_bytes)
        body = bytearray()
        while not too_large:
            message = await receive()
            if message["type"] != "http.request":  # client went away
                return
            body += message.get("body", b"")
            too_large = len(body) > self.max_bytes
            if not message.get("more_body"):
                break
        if too_large:
            err = TTSError("body_too_large", f"Request body exceeds {self.max_bytes} bytes.", 413,
                           max_bytes=self.max_bytes)
            return await JSONResponse({"error": err.to_dict()}, status_code=413)(scope, receive, send)
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()  # e.g. waiting for http.disconnect
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}
        await self.app(scope, replay, send)


def _gpu_memory() -> Optional[dict]:
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info()
        return {"allocated_gib": round(torch.cuda.memory_allocated() / 2**30, 2),
                "reserved_gib": round(torch.cuda.memory_reserved() / 2**30, 2),
                "device_used_gib": round((total - free) / 2**30, 2),
                "device_total_gib": round(total / 2**30, 2)}
    except Exception:  # noqa: BLE001 - health must not fail on GPU queries
        return None


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_server_config()
    uvicorn.run(build_app(), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
