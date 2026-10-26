"""HTTP client for tts-server, shared by the `tts` CLI and the `tts-mcp` server.

The server only returns audio; this module writes it to the caller's path:
- paths must be absolute; Windows paths (C:\\..., \\\\wsl$\\...) are converted with `wslpath`
- an existing target is an error unless overwrite=True
- no path -> ~/.local/share/local-tts/output/<timestamp>.<format>
- the format follows the file extension (.wav/.flac/.mp3) unless given explicitly

The client is async: every request runs under asyncio.timeout(), which cancels it while waiting on
the network (connect, headers, body) and closes the connection. Cancellation is cooperative: CPU work
between awaits (decoding a gzip body a server sends despite `Accept-Encoding: identity`, the local
file write) is not interrupted; tts-server does not compress, so only the write remains (ms). Waiting is bounded by a Deadline shared
by all requests of one call, so a caller (e.g. an MCP tool call) gets an answer — with the job id —
before its own timeout.
"""
import asyncio
import os
import re
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from local_tts.text import estimate_seconds

DEFAULT_URL = os.environ.get("LOCAL_TTS_URL", "http://127.0.0.1:8765")
OUTPUT_DIR = Path(os.environ.get("LOCAL_TTS_OUTPUT_DIR", Path.home() / ".local/share/local-tts/output")).expanduser()
FORMATS = ("wav", "flac", "mp3")
# mkstemp files are 0600; give written audio the normal permissions (0666 minus umask) instead.
_UMASK = os.umask(0)
os.umask(_UMASK)
WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
# Real-time factor (synthesis seconds per estimated audio second; measured ~0.6 per real audio second,
# and cloned voices run longer than the text estimate) and cold model load, for wait estimates.
RTF = 0.75
LOAD_SECONDS = 20
# Per-request wall-clock limits: polls are short; the audio download happens once, after completion.
POLL_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 30.0
# Grace after a call's deadline shared by its requests (see Deadline.request_timeout).
MIN_REQUEST_SECONDS = 0.5
# A submission whose response was lost is retried (same Idempotency-Key) this many times in all.
SUBMIT_ATTEMPTS = 3

EXISTS_HINT = "set overwrite (MCP: overwrite=true, CLI: --overwrite) to replace it"


class ClientError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def as_client_error(e: Exception) -> ClientError:
    """Map expected local failures (I/O, bad text encoding) to structured errors."""
    if isinstance(e, ClientError):
        return e
    if isinstance(e, OSError):
        return ClientError("io_error", f"{type(e).__name__}: {e}", errno=e.errno, file=e.filename)
    if isinstance(e, UnicodeError):
        return ClientError("invalid_input", f"Text is not valid UTF-8 ({e}).")
    return ClientError("internal_error", f"{type(e).__name__}: {e}")


def resumable(err: ClientError) -> bool:
    """Can waiting on the job again help? Not for a failed job or one the service no longer knows."""
    return err.details.get("job_status") != "failed" and err.code != "unknown_job"


class Deadline:
    """Shared time budget for one call (None = unlimited)."""

    def __init__(self, seconds: Optional[float]):
        self.end = None if seconds is None else time.monotonic() + max(0.0, seconds)

    def remaining(self) -> Optional[float]:
        return None if self.end is None else max(0.0, self.end - time.monotonic())

    def expired(self) -> bool:
        return self.end is not None and time.monotonic() >= self.end

    def request_timeout(self, cap: float) -> float:
        """Timeout for the next request: capped, and ending by the deadline plus MIN_REQUEST_SECONDS
        of grace, so a request that must be made (the submission with wait_seconds=0) can still
        complete. All requests of a call share that grace: the call's network waits end at most
        MIN_REQUEST_SECONDS after its deadline (plus the audio download of a finished job)."""
        if self.end is None:
            return cap
        return max(0.01, min(cap, self.end + MIN_REQUEST_SECONDS - time.monotonic()))


def resolve_output_path(path: Optional[str], fmt: Optional[str]) -> tuple[Path, str]:
    """Return (absolute path, format). Raises ClientError for unusable paths."""
    if fmt is not None and fmt not in FORMATS:
        raise ClientError("unsupported_format", f"Unsupported format '{fmt}'.", available=list(FORMATS))
    if not path:
        if not OUTPUT_DIR.is_absolute():
            raise ClientError("invalid_path", f"LOCAL_TTS_OUTPUT_DIR must be absolute, got '{OUTPUT_DIR}'.")
        fmt = fmt or "wav"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        return OUTPUT_DIR / f"{stamp}.{fmt}", fmt
    if WINDOWS_PATH_RE.match(path):
        proc = subprocess.run(["wslpath", "-u", path], capture_output=True, text=True)
        if proc.returncode != 0:
            raise ClientError("invalid_path", f"Cannot convert Windows path '{path}': {proc.stderr.strip()}")
        path = proc.stdout.strip()
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ClientError("invalid_path", f"output_path must be absolute, got '{path}'.")
    if path.endswith(("/", "\\")) or p.name in ("", ".", ".."):
        raise ClientError("invalid_path", f"output_path must name a file, got '{path}'.")
    ext = p.suffix.lower().lstrip(".")
    if ext in FORMATS:
        if fmt is not None and fmt != ext:
            raise ClientError("format_mismatch", f"format '{fmt}' does not match extension '.{ext}'.")
        fmt = ext
    elif ext:
        raise ClientError("unsupported_format", f"Unsupported extension '.{ext}'; use .wav, .flac or .mp3.",
                          available=list(FORMATS))
    else:
        fmt = fmt or "wav"
        p = p.with_name(f"{p.name}.{fmt}")
    return p, fmt


def check_target(path: Path, overwrite: bool) -> None:
    if path.is_dir():
        raise ClientError("invalid_path", f"'{path}' is a directory.")
    if path.exists() and not overwrite:
        raise ClientError("output_exists", f"'{path}' already exists; {EXISTS_HINT}.", path=str(path))


def write_file(path: Path, data: bytes, overwrite: bool) -> None:
    """Write atomically; without overwrite, never replace a file that appeared meanwhile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o666 & ~_UMASK)
        if overwrite:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)  # fails if the target exists
            except FileExistsError:
                raise ClientError("output_exists", f"'{path}' already exists; {EXISTS_HINT}.",
                                  path=str(path)) from None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def raise_job_error(job: dict) -> None:
    """Raise a failed job's structured server error as a ClientError. job_status="failed" marks it
    terminal: waiting on the job again cannot help, a corrected request must be submitted."""
    if job["status"] == "failed":
        err = dict(job["error"])
        raise ClientError(err.pop("code"), err.pop("message"), job_id=job["id"], job_status="failed", **err)


def estimate_remaining(text: Optional[str], job: dict[str, Any], model_loaded: Optional[bool]) -> Optional[float]:
    """Rough seconds until a job finishes (for agents deciding how long to wait); None if unknown."""
    if not text:
        return None
    total = estimate_seconds(text) * RTF
    p = job.get("progress") or {}
    if p.get("segments_total"):
        total *= 1 - p["segments_done"] / p["segments_total"]
    if job.get("status") == "queued":
        total += (job.get("queue_position") or 1) * 30  # unknown work ahead: assume ~30 s each
    if model_loaded is False and job.get("status") in ("queued", "running"):
        total += LOAD_SECONDS
    return round(total, 1)


class TTSClient:
    """Async client. `transport` is for tests (e.g. httpx.ASGITransport)."""

    def __init__(self, base_url: str = DEFAULT_URL, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._http: Optional[httpx.AsyncClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _client(self) -> httpx.AsyncClient:
        # An AsyncClient belongs to one event loop; make a new one if the loop changed.
        loop = asyncio.get_running_loop()
        if self._http is None or self._loop is not loop:
            # identity: no Content-Encoding to decode (the server does not compress anyway).
            self._http = httpx.AsyncClient(base_url=self.base_url, transport=self._transport, timeout=None,
                                           headers={"Accept-Encoding": "identity"})
            self._loop = loop
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and self._loop is asyncio.get_running_loop():
            await self._http.aclose()
        self._http = None

    async def _request(self, method: str, path: str, timeout: float = POLL_TIMEOUT, **kw) -> httpx.Response:
        """One request, fully read, that ends (connection closed) within `timeout` seconds."""
        try:
            async with asyncio.timeout(timeout):
                r = await self._client().request(method, path, **kw)
        except TimeoutError:
            raise ClientError("server_timeout", f"tts-server at {self.base_url} did not answer {method} {path} "
                              f"within {timeout:.1f}s.") from None
        except httpx.TransportError as e:
            raise ClientError("server_unreachable", f"tts-server not reachable at {self.base_url} ({e}). "
                              "Start it with: systemctl --user start local-tts") from None
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
            except Exception:  # noqa: BLE001 - non-JSON error body
                err = {"code": "http_error", "message": f"HTTP {r.status_code}: {r.text[:300]}"}
            raise ClientError(err.pop("code"), err.pop("message"), http_status=r.status_code, **err)
        return r

    async def health(self, timeout: float = POLL_TIMEOUT) -> dict:
        return (await self._request("GET", "/health", timeout=timeout)).json()

    async def models(self) -> list[dict]:
        return (await self._request("GET", "/v1/models")).json()["data"]

    async def voices(self, model: Optional[str] = None) -> list[dict]:
        return (await self._request("GET", "/v1/voices", params={"model": model} if model else None)).json()["data"]

    async def submit(self, text: str, *, model=None, voice=None, language=None, fmt="wav", seed=None,
                     deadline: Optional[Deadline] = None) -> dict:
        """Create a job. The request carries an Idempotency-Key, so when the response is lost (the
        server may have accepted the job) retrying returns that same job instead of generating twice."""
        body = {"input": text, "model": model, "voice": voice, "language": language,
                "response_format": fmt, "seed": seed}
        kw = {"json": {k: v for k, v in body.items() if v is not None},
              "headers": {"Idempotency-Key": uuid.uuid4().hex}}
        deadline = deadline or Deadline(None)
        for attempt in range(SUBMIT_ATTEMPTS):
            try:
                return (await self._request("POST", "/v1/jobs", timeout=deadline.request_timeout(POLL_TIMEOUT),
                                            **kw)).json()
            except ClientError as e:
                left = deadline.remaining()
                if (e.code not in ("server_unreachable", "server_timeout") or attempt == SUBMIT_ATTEMPTS - 1
                        or (left is not None and left < 1.0)):  # retrying would overrun the caller's deadline
                    raise
            await asyncio.sleep(0.5)

    async def job(self, job_id: str, timeout: float = POLL_TIMEOUT) -> dict:
        return (await self._request("GET", f"/v1/jobs/{job_id}", timeout=timeout)).json()

    async def wait(self, job_id: str, deadline: Deadline, poll: float = 1.0, last: Optional[dict] = None,
                   on_progress: Optional[Callable[[dict], None]] = None) -> dict:
        """Poll until the job finishes or the deadline passes; return the latest snapshot.
        A poll cut off by the deadline returns `last` (the job is still pending); other failures raise."""
        while True:
            # A finished job, or no time left: report what we know instead of asking again.
            if last is not None and (last["status"] in ("succeeded", "failed") or deadline.expired()):
                return last
            try:
                j = await self.job(job_id, timeout=deadline.request_timeout(POLL_TIMEOUT))
            except ClientError as e:
                if e.code == "server_timeout" and last is not None and deadline.remaining() is not None \
                        and deadline.remaining() < 1.0:
                    return last
                raise
            last = j
            if on_progress:
                on_progress(j)
            if j["status"] in ("succeeded", "failed") or deadline.expired():
                return j
            r = deadline.remaining()
            await asyncio.sleep(poll if r is None else min(poll, r))

    async def save(self, job: dict, path: Path, overwrite: bool) -> dict:
        """Download a succeeded job's audio to `path`; return the result metadata."""
        raise_job_error(job)
        r = job["result"]
        if path.suffix.lower().lstrip(".") != r["format"]:
            raise ClientError("format_mismatch", f"Job audio is {r['format']} but the output path is '{path}'.",
                              job_id=job["id"])
        data = (await self._request("GET", f"/v1/jobs/{job['id']}/audio", timeout=DOWNLOAD_TIMEOUT)).content
        # Written synchronously on purpose: a cancelled call can then never leave a write running in a
        # thread after the caller released its lock (writing a few MB locally takes milliseconds).
        write_file(path, data, overwrite)
        out = {"status": "completed", "path": str(path), "duration_seconds": r["duration"],
                "sample_rate": r["sample_rate"], "channels": r["channels"], "format": r["format"],
                "model": r["model"], "voice": r["voice"], "language": r["language"],
                "segments": r["segments"], "job_id": job["id"]}
        if r.get("warnings"):  # e.g. a segment that stayed suspiciously short (a sentence may be missing)
            out["warnings"] = r["warnings"]
        return out

    async def pending(self, job: dict, text: Optional[str], deadline: Optional[Deadline] = None) -> dict:
        """A pending result. Whether the model is loaded (for the estimate) is asked only while the
        call's deadline has not passed; otherwise it is unknown."""
        loaded = None
        if deadline is None or not deadline.expired():
            try:
                timeout = 2.0 if deadline is None else min(2.0, deadline.remaining() or 0.01)
                loaded = (await self.health(timeout=timeout)).get("loaded_model") is not None
            except ClientError:
                pass
        return {"status": "pending", "job_id": job["id"], "job_status": job["status"],
                "queue_position": job.get("queue_position"), "progress": job.get("progress"),
                "estimated_remaining_seconds": estimate_remaining(text, job, loaded)}
