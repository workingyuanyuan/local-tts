from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from local_tts.config import ModelEntry, VoiceEntry
from local_tts.errors import TTSError


@dataclass
class SegmentAudio:
    audio: np.ndarray       # float32 [channels, samples]
    sample_rate: int
    frames: int             # generated codec frames
    truncated: bool         # hit the frame budget without an end token
    reference: Any = None   # adapter-specific handle reusable as a voice reference


class Adapter(ABC):
    """One TTS model. All methods except the class attributes run on the GPU worker thread."""

    languages: dict[str, str] = {}       # code -> name
    capabilities: dict[str, bool] = {}
    frames_per_second: float = 12.5
    # Per-request options (beyond model/input/voice/response_format) this adapter accepts.
    supported_options: set[str] = set()

    def __init__(self, entry: ModelEntry):
        self.entry = entry

    @property
    @abstractmethod
    def loaded(self) -> bool: ...

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def unload(self) -> None: ...

    @abstractmethod
    def voice_reference(self, voice: VoiceEntry) -> Any:
        """Prepared reference for a registered voice (may be cached)."""

    @abstractmethod
    def generate(self, text: str, *, language: Optional[str], reference: Any,
                 max_frames: int, seed: Optional[int]) -> SegmentAudio: ...

    def normalize_language(self, language: Optional[str]) -> Optional[str]:
        """Accept a language code or name (case-insensitive); return the code."""
        if language is None:
            return None
        key = language.strip().lower()
        for code, name in self.languages.items():
            if key in (code, name.lower()):
                return code
        raise TTSError("unsupported_language", f"Model '{self.entry.id}' does not support language '{language}'.",
                       400, available=sorted(self.languages))
