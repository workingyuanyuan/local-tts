"""tts-mcp: MCP (stdio) front end for tts-server.

Waiting in a tool call (submit + polling) is bounded by `wait_seconds` (default 45 s, under Codex's
default 60 s MCP tool timeout; max 240 s). Only a finished job adds the audio download (<= 30 s).
A long narration returns status "pending" with a job_id; call get_speech_job(job_id) to keep
waiting. Jobs are never resubmitted; once a job is known, every error carries its job_id.
"""
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from local_tts.client import (ClientError, Deadline, TTSClient, as_client_error, check_target, resolve_output_path,
                              resumable)

DEFAULT_WAIT = 45.0
MAX_WAIT = 240.0

INSTRUCTIONS = """Local text-to-speech (runs on this machine's GPU). Use it whenever a task needs spoken
audio: video narration, slide voice-over, reading teaching material aloud, audio previews.
- Write output into the task's project directory with an absolute output_path (.wav/.flac/.mp3).
- Pass language (e.g. "zh", "en") when known. Omit voice for the model's default voice, or pick one
  from list_voices to clone it. Omit model to use the default.
- Text may contain [pause 1.5s] markers; blank lines are paragraph breaks. Long text is fine.
- Long narrations return status "pending" with "next": keep calling get_speech_job with exactly
  next.arguments (job_id, the same absolute output_path, overwrite if it was set) until "completed"
  (always pass the path: after an MCP restart the default location is outside the project). Never call
  generate_speech again for the same text while a job is pending. If an error carries a job_id,
  follow its "recovery" field: resume interrupted calls the same way; a failed job (job_status
  "failed") or an unknown_job needs a new generate_speech call.
- A completed result with "warnings" may be missing a sentence: regenerate it once (overwrite=true)."""

# Writing tools create files; with overwrite=true they replace one, hence destructive. Local only.
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)

log = logging.getLogger("local_tts.mcp")


class StrictServer(MCPServer):
    """Unknown tool arguments are an explicit `unsupported_parameter` error, never silently dropped
    (the schemas also say so with additionalProperties: false)."""

    async def list_tools(self):
        tools = await super().list_tools()
        for t in tools:
            t.input_schema["additionalProperties"] = False
        return tools

    async def call_tool(self, name, arguments, context=None):
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            known = set(tool.parameters.get("properties", {}))
            unknown = sorted(set(arguments) - known)
            if unknown:
                raise ToolError(json.dumps({"error": {
                    "code": "unsupported_parameter", "message": f"Unsupported parameter(s): {', '.join(unknown)}.",
                    "parameters": unknown, "supported": sorted(known)}}, ensure_ascii=False))
        return await super().call_tool(name, arguments, context)


server = StrictServer(name="local-tts", instructions=INSTRUCTIONS)
client = TTSClient()


@dataclass
class JobEntry:
    """Everything this process knows about one job. The lock is created once per job and guards the
    path decision and the save, so concurrent or repeated get_speech_job calls save each path once."""
    text: Optional[str] = None            # None when recovered after a restart (no time estimate then)
    fmt: Optional[str] = None             # the job's audio format (known at submit or from the server)
    default_path: Optional[Path] = None   # output_path given to generate_speech
    default_overwrite: bool = False
    saved: dict[Path, dict] = field(default_factory=dict)  # path -> result, for idempotent repeats
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    used: float = field(default_factory=time.monotonic)


# All tools run on the server's single event loop, so plain dict access is safe.
JOBS: dict[str, JobEntry] = {}
# Entries unused this long are dropped (tts-server forgets finished jobs after an hour by default).
JOB_MEMORY_SECONDS = 2 * 3600


def _entry(job_id: str) -> JobEntry:
    now = time.monotonic()
    for jid in [j for j, e in JOBS.items() if now - e.used > JOB_MEMORY_SECONDS and not e.lock.locked()]:
        del JOBS[jid]
    entry = JOBS.setdefault(job_id, JobEntry())
    entry.used = now
    return entry


def _forget(job_id: str, e: Exception) -> None:
    """The service no longer knows this job: keeping our entry would only grow JOBS."""
    entry = JOBS.get(job_id)
    if isinstance(e, ClientError) and e.code == "unknown_job" and entry is not None and not entry.lock.locked():
        del JOBS[job_id]


def _error(e: Exception, **context: Any) -> ToolError:
    """Every failure becomes {"error": {"code", "message", ...}} (plus job context when known)."""
    if not isinstance(e, (ClientError, OSError, UnicodeError)):
        log.exception("unexpected tool failure")
    ce = as_client_error(e)
    err = ce.to_dict()
    if not resumable(ce):  # failed or forgotten: get_speech_job would only repeat this error
        context["recovery"] = ("The job failed or the service no longer has it (restarted or expired); it cannot "
                               "be resumed. Fix the cause above if there is one (e.g. rephrase or shorten the "
                               "named segment) and call generate_speech again.")
    elif context.get("job_id"):
        args = _resume_args(context["job_id"], context.get("output_path"), context.get("overwrite", False))
        context["recovery"] = (f"The job may still be running; call get_speech_job with "
                               f"{json.dumps(args, ensure_ascii=False)} instead of resubmitting.")
    context.pop("overwrite", None)
    err.update({k: v for k, v in context.items() if v is not None and k not in err})
    return ToolError(json.dumps({"error": err}, ensure_ascii=False))


def _clamp_wait(wait_seconds: float) -> float:
    return max(0.0, min(float(wait_seconds), MAX_WAIT))


def _resume_args(job_id: str, path, overwrite: bool) -> dict[str, Any]:
    """get_speech_job arguments that finish the job as requested, even after an MCP restart."""
    args: dict[str, Any] = {"job_id": job_id}
    if path is not None:
        args["output_path"] = str(path)
    if overwrite:
        args["overwrite"] = True
    return args


def _pending(job_id: str, pend: dict, path: Optional[Path], overwrite: bool) -> dict[str, Any]:
    """A pending result names its target file and the exact call to keep waiting."""
    if path is None:
        return pend
    return pend | {"output_path": str(path),
                   "next": {"tool": "get_speech_job", "arguments": _resume_args(job_id, path, overwrite)}}


def _target(entry: JobEntry, output_path: Optional[str], overwrite: bool) -> tuple[Optional[Path], bool]:
    """The file a call writes and whether it may replace it. Naming generate_speech's own path again
    keeps its overwrite permission (as `next` does), so following `next` finishes a regeneration."""
    if output_path is None:
        return entry.default_path, overwrite or (entry.default_path is not None and entry.default_overwrite)
    path, _ = resolve_output_path(output_path, entry.fmt)  # format_mismatch if the extension differs
    return path, overwrite or (path == entry.default_path and entry.default_overwrite)


async def _deliver(job_id: str, entry: JobEntry, output_path: Optional[str], overwrite: bool, deadline: Deadline,
                   last: Optional[dict] = None) -> dict[str, Any]:
    """Under the job's lock: pick the target path, wait (within the deadline) and save it once."""
    try:
        async with asyncio.timeout(deadline.remaining() or 0.01):
            await entry.lock.acquire()
    except TimeoutError:  # another call for this job holds the lock until its own deadline
        pend = await client.pending(last or await client.job(job_id, timeout=deadline.request_timeout(10.0)),
                                    entry.text, deadline)
        path = Path(output_path) if output_path else entry.default_path
        return _pending(job_id, pend, path, overwrite or (path == entry.default_path and entry.default_overwrite))
    try:
        if entry.fmt is None:  # recovered after a restart: ask the server for the job's format
            last = await client.job(job_id, timeout=deadline.request_timeout(10.0))
            entry.fmt = last["format"]
        path, overwrite = _target(entry, output_path, overwrite)
        if path is None:  # recovered without a path: keep using the same default file
            path, _ = resolve_output_path(None, entry.fmt)
            entry.default_path = path
        if path in entry.saved:
            return entry.saved[path]
        job = await client.wait(job_id, deadline, last=last)
        if job["status"] in ("queued", "running"):
            return _pending(job_id, await client.pending(job, entry.text, deadline), path, overwrite)
        result = await client.save(job, path, overwrite)
        entry.saved[path] = result
        log.info("job %s saved to %s", job_id, path)
        return result
    finally:
        entry.lock.release()


async def _generate(text, output_path, language, voice, model, fmt, overwrite, seed, wait_seconds) -> dict[str, Any]:
    deadline = Deadline(_clamp_wait(wait_seconds))
    path, fmt = resolve_output_path(output_path, fmt)
    check_target(path, overwrite)
    job = await client.submit(text, model=model, voice=voice, language=language, fmt=fmt, seed=seed,
                              deadline=deadline)
    entry = _entry(job["id"])
    entry.text, entry.fmt, entry.default_path, entry.default_overwrite = text, fmt, path, overwrite
    log.info("job %s submitted -> %s", job["id"], path)
    try:
        return await _deliver(job["id"], entry, None, False, deadline, last=job)
    except Exception as e:  # noqa: BLE001 - attach the job so the caller can recover
        _forget(job["id"], e)
        raise _error(e, job_id=job["id"], output_path=str(path), overwrite=overwrite) from None


async def _get_job(job_id, wait_seconds, output_path, overwrite) -> dict[str, Any]:
    deadline = Deadline(_clamp_wait(wait_seconds))
    entry = _entry(job_id)
    try:
        return await _deliver(job_id, entry, output_path, overwrite, deadline)
    except Exception as e:  # noqa: BLE001
        _forget(job_id, e)
        try:
            path, overwrite = _target(entry, output_path, overwrite)
        except ClientError:
            path = output_path
        raise _error(e, job_id=job_id, output_path=str(path) if path else None, overwrite=overwrite) from None


async def _run(fn, *args) -> Any:
    try:
        return await fn(*args)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001 - structured error for every failure
        raise _error(e) from None


@server.tool(annotations=WRITES)
async def generate_speech(
    text: str,
    output_path: Optional[str] = None,
    language: Optional[str] = None,
    voice: Optional[str] = None,
    model: Optional[str] = None,
    format: Optional[Literal["wav", "flac", "mp3"]] = None,
    overwrite: bool = False,
    seed: Optional[int] = None,
    wait_seconds: float = DEFAULT_WAIT,
) -> dict[str, Any]:
    """Synthesize speech from text and write it to an audio file.

    Args:
        text: Text to speak. Supports [pause 1.5s] markers; blank lines separate paragraphs.
        output_path: Absolute path of the file to write (.wav, .flac or .mp3), normally inside the
            task's project directory. Windows paths (C:\\...) are accepted. Default:
            ~/.local/share/local-tts/output/<timestamp>.wav
        language: Language code or name, e.g. "zh", "en", "ja" (recommended when known).
        voice: Voice name from list_voices to clone. Omit for the model's default voice.
        model: Model id from list_models. Omit for the default model.
        format: wav | flac | mp3. Inferred from the output_path extension when omitted.
        overwrite: Replace output_path if it already exists (otherwise that is an error).
        seed: Optional integer (0..2**32-1) for reproducible output.
        wait_seconds: How long this call waits for the result (max 240). If synthesis takes longer,
            the result is status "pending" with a job_id: call get_speech_job(job_id) to keep waiting.

    Returns: status "completed" with path, duration_seconds, sample_rate, channels, format, model,
    voice (plus "warnings" when a segment stayed suspiciously short: a sentence may be missing, so
    regenerate once with overwrite=true); or status "pending" with job_id, output_path, progress, estimated_remaining_seconds and
    "next" ({"tool": "get_speech_job", "arguments": {...}}: the call to keep waiting). Errors are
    JSON {"error": {"code", "message", ...}}; once the job exists they include job_id, output_path
    and recovery (how to resume, or job_status "failed" when only a new request can help).
    """
    return await _run(_generate, text, output_path, language, voice, model, format, overwrite, seed, wait_seconds)


@server.tool(annotations=WRITES)
async def get_speech_job(
    job_id: str,
    wait_seconds: float = DEFAULT_WAIT,
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Wait for a pending generate_speech job and write its audio when done. Calling it again
    after completion returns the same result.

    Args:
        job_id: The job_id returned by generate_speech (status "pending" or in an error).
        wait_seconds: How long this call waits (max 240) before returning "pending" again.
        output_path: The absolute path given to generate_speech; always pass it (as in the pending
            result's "next"). Omitted, it is generate_speech's path, or after an MCP server restart a
            default file outside the project. Its extension must match the job's format.
        overwrite: Replace an existing file at the target path (e.g. to retry after output_exists).
            Passing generate_speech's own path keeps that call's overwrite setting.
    """
    return await _run(_get_job, job_id, wait_seconds, output_path, overwrite)


@server.tool(annotations=READS)
async def list_models() -> list[dict[str, Any]]:
    """List TTS models: id, whether it is the default, whether it is loaded, supported languages,
    capabilities (clone, pause_marker, pinyin, ...) and accepted options."""
    models = await _run(client.models)
    return [{k: m[k] for k in ("id", "description", "default", "loaded", "languages", "capabilities", "options")}
            for m in models]


@server.tool(annotations=READS)
async def list_voices(model: Optional[str] = None) -> list[dict[str, Any]]:
    """List voices that can be cloned: name, description, language, compatible models.
    Pass model to only list voices compatible with it."""
    return await _run(client.voices, model)


@server.tool(annotations=READS)
async def tts_status() -> dict[str, Any]:
    """Service health: loaded model, queue length, running job, GPU memory."""
    return await _run(client.health)


def main() -> None:
    # stdout is the MCP channel: log to stderr only.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll otherwise
    server.run("stdio")


if __name__ == "__main__":
    main()
