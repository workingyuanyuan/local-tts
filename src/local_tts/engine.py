"""Model management and the long-text synthesis pipeline. Runs on the single GPU worker thread."""
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from local_tts.adapters import adapter_class
from local_tts.adapters.base import Adapter
from local_tts.audio import silence, trim_silence
from local_tts.config import ModelEntry, Registry, SegmentingConfig, VoiceEntry
from local_tts.errors import TTSError
from local_tts.text import Segment, estimate_seconds, split_finer, split_text

log = logging.getLogger(__name__)

# A segment shorter than this fraction of its estimate (minus slack) is treated as dropped speech.
MIN_DURATION_RATIO = 0.3
# Below this fraction a sentence was probably skipped (seen: 0.66x for a dropped English sentence;
# normal speech was >= 0.75x over 129 logged segments): retry, and if every attempt stays short keep
# the longest one with a warning instead of failing.
SUSPECT_DURATION_RATIO = 0.7
# A segment longer than this multiple of its estimate (plus slack) is treated as run-on output (the
# model "talking" past the end of the text). A defensive check: not observed so far. Calibrated on
# 68 real segments: normal speech is 1.0-1.1x (English) to ~1.5x (Chinese), at most 2.14x.
MAX_DURATION_RATIO = 2.2
MAX_DURATION_SLACK = 3.0
# A segment reused as the speaker reference for later segments must be at least this long.
MIN_REFERENCE_SECONDS = 3.0


class ModelManager:
    """Holds at most one loaded model; load/unload are serialized by a lock."""

    def __init__(self, registry: Registry):
        self.registry = registry
        self.adapters: dict[str, Adapter] = {
            m.id: adapter_class(m.adapter)(m) for m in registry.models.values()
        }
        self.current: Optional[Adapter] = None
        self.lock = threading.Lock()

    def adapter(self, model_id: str) -> Adapter:
        return self.adapters[model_id]

    def acquire(self, model_id: str) -> Adapter:
        with self.lock:
            target = self.adapters[model_id]
            if self.current is target and target.loaded:
                return target
            if self.current is not None:
                # 24 GB is not guaranteed to hold two models: unload before switching.
                log.info("unloading %s before loading %s", self.current.entry.id, model_id)
                self.current.unload()
                self.current = None
            t = time.perf_counter()
            log.info("loading %s", model_id)
            error = None
            try:
                target.load()
            except Exception as e:  # noqa: BLE001 - any load failure is reported as model_load_failed
                log.exception("loading %s failed", model_id)
                error = TTSError("model_load_failed", f"Failed to load model '{model_id}': "
                                 f"{type(e).__name__}: {e}", 500)
            if error is not None:
                # Raised outside the except block so no traceback (and no partially loaded GPU
                # tensor referenced from its frames) stays attached to the error.
                target.unload()
                raise error
            log.info("loaded %s in %.1fs", model_id, time.perf_counter() - t)
            self.current = target
            return target

    def unload(self) -> Optional[str]:
        with self.lock:
            if self.current is None:
                return None
            model_id = self.current.entry.id
            self.current.unload()
            self.current = None
            log.info("unloaded %s", model_id)
            return model_id

    @property
    def loaded_model(self) -> Optional[str]:
        cur = self.current
        return cur.entry.id if cur is not None and cur.loaded else None


@dataclass
class SynthesisRequest:
    model: ModelEntry
    voice: Optional[VoiceEntry]
    text: str
    response_format: str = "wav"
    language: Optional[str] = None  # normalized code
    seed: Optional[int] = None


@dataclass
class SynthesisResult:
    audio: np.ndarray
    sample_rate: int
    segments: int
    retries: int = 0
    splits: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.audio.shape[1] / self.sample_rate


class _Synthesis:
    def __init__(self, adapter: Adapter, req: SynthesisRequest, cfg: SegmentingConfig,
                 progress: Optional[Callable[[int, int], None]]):
        self.adapter, self.req, self.cfg, self.progress = adapter, req, cfg, progress
        self.retries = 0
        self.splits = 0
        self.warnings: list[tuple[list, str]] = []  # (slot, warning): dropped if the slot is re-rendered
        self.sample_rate = 0
        self.channels = 2
        self.reference: Any = None
        # Without a voice, the first good piece (>= MIN_REFERENCE_SECONDS) becomes the reference.
        self.follow_first = False
        # Shorter pieces rendered before that (e.g. a title) are re-rendered with the reference once it
        # exists, so the whole result keeps one voice: (slot, text, seg_index, depth, seed).
        self.early: list[tuple[list, str, int, int, Optional[int]]] = []

    def _budget(self, text: str) -> int:
        est = estimate_seconds(text)
        frames = math.ceil((est * 2.5 + 10) * self.adapter.frames_per_second)
        # At least 10 s, but never above the configured hard cap.
        return min(int(self.adapter.entry.options.get("max_new_tokens", 4096)), max(125, frames))

    def _silence(self, seconds: float) -> np.ndarray:
        return silence(self.channels, self.sample_rate, seconds)

    def _sequence(self, segments: list[Segment], seg_index: Optional[int], depth: int,
                  seed: Optional[int]) -> list[list[np.ndarray]]:
        """Render segments in order with gaps; edge [pause] silences are kept. `seg_index` is the
        top-level segment being re-split (None at the top level, where each segment is its own).
        Returns one-element slots (concatenated by run()), so an early piece can be replaced later."""
        out: list[list[np.ndarray]] = []
        for i, seg in enumerate(segments):
            index = i if seg_index is None else seg_index
            s = None if seed is None else seed + (1000 if seg_index is None else 100) * (i + 1)
            slots = self._render(seg.text, index, depth, s)
            gap = seg.lead_silence
            if i > 0:
                prev = segments[i - 1]
                between = self.cfg.paragraph_gap_seconds if prev.paragraph_end else self.cfg.segment_gap_seconds
                gap += prev.trail_silence + between
            if gap > 0:
                out.append([self._silence(gap)])
            out.extend(slots)
            if seg_index is None and self.progress:
                self.progress(i + 1, len(segments))
        if segments[-1].trail_silence > 0:
            out.append([self._silence(segments[-1].trail_silence)])
        return out

    def _render(self, text: str, seg_index: int, depth: int, seed: Optional[int]) -> list[list[np.ndarray]]:
        """Generate one piece of text; on truncation split it and render the halves."""
        est = estimate_seconds(text)
        last_problem = ""
        best = None  # (dur, audio, reference): the longest suspiciously short attempt
        for attempt in range(self.cfg.max_retries + 1):
            s = None if seed is None else seed + attempt
            try:
                r = self.adapter.generate(text, language=self.req.language, reference=self.reference,
                                          max_frames=self._budget(text), seed=s)
            except Exception as e:
                if not isinstance(e, TTSError) and not hasattr(e, "tts_segment"):
                    e.tts_segment = seg_index  # the scheduler reports which segment crashed (e.g. OOM)
                raise
            self.sample_rate, self.channels = r.sample_rate, r.audio.shape[0] or 2
            if r.truncated:
                parts = split_finer(text)
                if depth >= self.cfg.max_split_depth or len(parts) < 2:
                    raise TTSError("segment_truncated",
                                   f"Segment {seg_index} still truncated after splitting {depth} time(s).",
                                   500, segment=seg_index, text=text)
                log.warning("segment %d truncated at %d frames; splitting into %d parts",
                            seg_index, r.frames, len(parts))
                self.splits += 1
                return self._sequence(parts, seg_index, depth + 1, seed)
            audio = trim_silence(r.audio, r.sample_rate)
            dur = audio.shape[1] / r.sample_rate
            log.info("segment %d attempt %d: %.1fs audio for ~%.1fs estimate (%d chars)",
                     seg_index, attempt, dur, est, len(text))
            if audio.shape[1] == 0 or (est > 2 and dur < MIN_DURATION_RATIO * est - 0.5):
                last_problem = f"got {dur:.1f}s audio for ~{est:.1f}s of text"
            elif dur > MAX_DURATION_RATIO * est + MAX_DURATION_SLACK:
                last_problem = f"got {dur:.1f}s audio for ~{est:.1f}s of text (run-on output)"
            elif est > 2 and dur < SUSPECT_DURATION_RATIO * est:
                last_problem = f"got {dur:.1f}s audio for ~{est:.1f}s of text (a sentence may be missing)"
                if best is None or dur > best[0]:
                    best = (dur, audio, r.reference)
            else:
                return self._accept(audio, dur, r.reference, text, seg_index, depth, seed)
            if attempt < self.cfg.max_retries:
                log.warning("segment %d attempt %d: %s; retrying", seg_index, attempt, last_problem)
                self.retries += 1
            else:
                log.warning("segment %d attempt %d: %s", seg_index, attempt, last_problem)
        if best is not None:
            dur, audio, reference = best
            warning = (f"segment {seg_index}: {dur:.1f}s of audio for ~{est:.1f}s of text after "
                       f"{self.cfg.max_retries} retries; a sentence may be missing")
            log.warning(warning)
            slots = self._accept(audio, dur, reference, text, seg_index, depth, seed)
            self.warnings.append((slots[0], warning))
            return slots
        raise TTSError("segment_failed", f"Segment {seg_index} failed after {self.cfg.max_retries} retries: "
                       f"{last_problem}.", 500, segment=seg_index, text=text)

    def _accept(self, audio: np.ndarray, dur: float, reference: Any, text: str, seg_index: int, depth: int,
                seed: Optional[int]) -> list[list[np.ndarray]]:
        slot = [audio]
        if self.follow_first and self.reference is None and reference is not None:
            if dur >= MIN_REFERENCE_SECONDS:
                self.reference = reference
            else:
                self.early.append((slot, text, seg_index, depth, seed))
        return [slot]

    def run(self) -> SynthesisResult:
        segments = split_text(self.req.text, self.cfg.max_segment_seconds)
        if not segments:
            raise TTSError("empty_input", "Input contains no speakable text.", 400)
        if self.req.voice:
            self.reference = self.adapter.voice_reference(self.req.voice)
        else:
            self.follow_first = self.cfg.unvoiced_consistency == "first_segment"
        slots = self._sequence(segments, None, 0, self.req.seed)
        if self.reference is not None:
            for slot, text, seg_index, depth, seed in self.early:
                log.info("segment %d: re-rendering a short early piece with the reference voice", seg_index)
                fresh = self._render(text, seg_index, depth, seed)
                slot[0] = np.concatenate([x[0] for x in fresh], axis=1)
                # the old take's warning no longer applies; the new take's warnings move to this slot
                self.warnings = [(slot if any(s is x for x in fresh) else s, w) for s, w in self.warnings
                                 if s is not slot]
        audio = np.concatenate([x[0] for x in slots], axis=1)
        return SynthesisResult(audio=audio, sample_rate=self.sample_rate, segments=len(segments),
                               retries=self.retries, splits=self.splits, warnings=[w for _, w in self.warnings])


def synthesize(adapter: Adapter, req: SynthesisRequest, cfg: SegmentingConfig,
               progress: Optional[Callable[[int, int], None]] = None) -> SynthesisResult:
    return _Synthesis(adapter, req, cfg, progress).run()
