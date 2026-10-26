"""Single GPU worker with a bounded FIFO queue, job tracking and idle unload.

One condition variable guards the pending queue and every job-state transition, so:
- dequeuing a job and marking it running is atomic (snapshots never show a queued job without a
  queue position);
- the idle-unload decision and the unload itself hold the lock, so a request submitted meanwhile
  waits for the unload to finish instead of racing it.
"""
import collections
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from local_tts.audio import FORMATS, encode
from local_tts.config import ServerConfig
from local_tts.engine import ModelManager, SynthesisRequest, synthesize
from local_tts.errors import TTSError

log = logging.getLogger(__name__)


@dataclass
class Job:
    request: SynthesisRequest
    key: Optional[str] = None  # client idempotency key: resubmitting with it returns this job
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    status: str = "queued"  # queued | running | succeeded | failed
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    segments_done: int = 0
    segments_total: Optional[int] = None
    audio: Optional[bytes] = None
    meta: dict[str, Any] = field(default_factory=dict)
    # Stored as plain data (not the exception) so no traceback keeps GPU objects alive.
    error: Optional[dict[str, Any]] = None
    error_http_status: int = 500
    done: threading.Event = field(default_factory=threading.Event)


class Scheduler:
    def __init__(self, manager: ModelManager, cfg: ServerConfig):
        self.manager = manager
        self.cfg = cfg
        self.cond = threading.Condition()
        self.pending: collections.deque[Job] = collections.deque()
        self.jobs: dict[str, Job] = {}
        self.keys: dict[str, Job] = {}
        self.running: Optional[Job] = None
        self.last_activity = time.monotonic()
        self.janitor_interval = 30.0
        self._stop = False
        self._thread = threading.Thread(target=self._worker, name="gpu-worker", daemon=True)
        # Expired jobs are purged here too, so audio is released even while a long job runs.
        self._janitor = threading.Thread(target=self._janitor_loop, name="job-janitor", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._janitor.start()

    def _janitor_loop(self) -> None:
        with self.cond:
            while not self._stop:
                self._purge()
                self.cond.wait(timeout=self.janitor_interval)

    def stop(self) -> None:
        with self.cond:
            self._stop = True
            self.cond.notify_all()
        self._thread.join(timeout=5)

    def submit(self, req: SynthesisRequest, key: Optional[str] = None) -> tuple[Job, bool]:
        """Queue a request; returns (job, created). A known `key` returns its existing job, so a
        client that lost the response to a submission can retry it without generating twice."""
        job = Job(request=req, key=key)
        with self.cond:
            if key is not None and key in self.keys:
                return self.keys[key], False
            if len(self.pending) >= self.cfg.queue_max:
                raise TTSError("queue_full", f"Queue is full ({self.cfg.queue_max} waiting); retry later.", 503,
                               queue_length=len(self.pending))
            held = self._held_bytes()
            if held >= self.cfg.max_result_bytes:
                raise TTSError("results_full", f"Finished audio not yet expired uses {held} bytes (limit "
                               f"{self.cfg.max_result_bytes}); retry later.", 503, held_bytes=held)
            self.pending.append(job)
            self.jobs[job.id] = job
            if key is not None:
                self.keys[key] = job
            self.cond.notify_all()
            queued = len(self.pending)
        log.info("job %s queued (model=%s, %d chars, queue=%d)", job.id, req.model.id, len(req.text), queued)
        return job, True

    def get(self, job_id: str) -> Job:
        with self.cond:
            job = self.jobs.get(job_id)
        if job is None:
            raise TTSError("unknown_job", f"Unknown or expired job '{job_id}'.", 404)
        return job

    def snapshot(self, job: Job) -> dict[str, Any]:
        """Consistent view of a job (taken under the scheduler lock)."""
        with self.cond:
            d = {
                "id": job.id, "status": job.status, "model": job.request.model.id,
                "voice": job.request.voice.name if job.request.voice else None,
                "format": job.request.response_format,
                "created": job.created, "started": job.started, "finished": job.finished,
                "progress": {"segments_done": job.segments_done, "segments_total": job.segments_total},
            }
            if job.status == "queued":
                d["queue_position"] = next(i for i, j in enumerate(self.pending, 1) if j is job)
            if job.status == "succeeded":
                d["result"] = dict(job.meta)
            if job.error is not None:
                d["error"] = dict(job.error)
            return d

    def outcome(self, job: Job) -> tuple[str, Optional[dict[str, Any]], int, Optional[bytes], dict[str, Any]]:
        """(status, error, error_http_status, audio, meta) read atomically."""
        with self.cond:
            return job.status, job.error, job.error_http_status, job.audio, dict(job.meta)

    def stats(self) -> dict[str, Any]:
        with self.cond:
            return {"queue_length": len(self.pending),
                    "running_job": self.running.id if self.running else None}

    def _held_bytes(self) -> int:
        """RAM used by finished audio waiting to be fetched. Caller holds the lock."""
        return sum(len(j.audio) for j in self.jobs.values() if j.audio is not None)

    def _purge(self) -> None:
        """Drop finished jobs (and their audio) older than the TTL. Caller holds the lock."""
        cutoff = time.time() - self.cfg.job_ttl_seconds
        for jid in [j.id for j in self.jobs.values() if j.finished and j.finished < cutoff]:
            job = self.jobs.pop(jid)
            if job.key is not None:
                self.keys.pop(job.key, None)

    def _worker(self) -> None:
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.pending or self._stop, timeout=1.0)
                if self._stop:
                    return
                self._purge()
                if not self.pending:
                    # Nothing running (single worker) and the queue is empty; submit() is blocked on
                    # the lock while we decide and unload.
                    if (self.manager.loaded_model
                            and time.monotonic() - self.last_activity >= self.cfg.idle_unload_seconds):
                        log.info("idle for %.0fs; unloading", time.monotonic() - self.last_activity)
                        self.manager.unload()
                    continue
                job = self.pending.popleft()
                job.status, job.started = "running", time.time()
                self.running = job
            self._run(job)
            job = None  # drop the reference so TTL purging can free the audio
            self.last_activity = time.monotonic()

    def _run(self, job: Job) -> None:
        req = job.request
        meta: dict[str, Any] = {}
        audio: Optional[bytes] = None
        error: Optional[TTSError] = None
        try:
            adapter = self.manager.acquire(req.model.id)

            def progress(done: int, total: int) -> None:
                with self.cond:
                    job.segments_done, job.segments_total = done, total

            t = time.perf_counter()
            res = synthesize(adapter, req, self.cfg.segmenting, progress)
            elapsed = time.perf_counter() - t
            audio = encode(res.audio, res.sample_rate, req.response_format)
            meta = {
                "duration": round(res.duration, 3), "sample_rate": res.sample_rate,
                "channels": int(res.audio.shape[0]), "format": req.response_format,
                "content_type": FORMATS[req.response_format], "bytes": len(audio),
                "model": req.model.id, "voice": req.voice.name if req.voice else None,
                "language": req.language, "segments": res.segments, "retries": res.retries,
                "splits": res.splits, "synthesis_seconds": round(elapsed, 2),
                "rtf": round(elapsed / res.duration, 3) if res.duration else None,
                "warnings": res.warnings,
            }
            log.info("job %s done: %.1fs audio in %.1fs (%d segments, %d retries, %d splits)",
                     job.id, res.duration, elapsed, res.segments, res.retries, res.splits)
        except TTSError as e:
            error = TTSError(e.code, e.message, e.http_status, **e.details)
            log.warning("job %s failed: %s %s", job.id, e.code, e.message)
        except Exception as e:  # noqa: BLE001 - report any worker failure to the client
            log.exception("job %s crashed", job.id)
            seg = getattr(e, "tts_segment", None)
            where = {} if seg is None else {"segment": seg}
            at = "" if seg is None else f" (segment {seg})"
            if type(e).__name__ == "OutOfMemoryError":
                error = TTSError("gpu_out_of_memory", f"GPU out of memory{at}: {e}", 500, **where)
            else:
                error = TTSError("internal_error", f"{type(e).__name__}{at}: {e}", 500, **where)
        if error is not None and error.code == "gpu_out_of_memory":
            import torch
            torch.cuda.empty_cache()
        with self.cond:
            job.finished = time.time()
            if error is None and self._held_bytes() + len(audio) > self.cfg.max_result_bytes:
                error = TTSError("results_full", f"The result ({len(audio)} bytes) does not fit in max_result_bytes "
                                 f"({self.cfg.max_result_bytes}) next to unexpired results; retry later.", 503,
                                 held_bytes=self._held_bytes(), result_bytes=len(audio))
            if error is None:
                job.audio, job.meta, job.status = audio, meta, "succeeded"
            else:
                job.error, job.error_http_status, job.status = error.to_dict(), error.http_status, "failed"
            self.running = None
        job.done.set()
