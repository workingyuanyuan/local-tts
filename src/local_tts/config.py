"""Load server settings and the model / voice registries from config/*.yaml."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from local_tts.errors import TTSError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("LOCAL_TTS_CONFIG_DIR", PROJECT_ROOT / "config"))


@dataclass
class SegmentingConfig:
    max_segment_seconds: float = 30.0
    segment_gap_seconds: float = 0.35
    paragraph_gap_seconds: float = 0.8
    unvoiced_consistency: str = "first_segment"  # or "none"
    max_retries: int = 2
    max_split_depth: int = 2


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    queue_max: int = 8
    idle_unload_seconds: float = 600
    job_ttl_seconds: float = 3600
    # Finished audio kept for polling may use at most this much RAM; beyond it submissions get 503.
    max_result_bytes: int = 4 * 2**30
    # /v1/audio/speech waits at most this long, then returns 504 sync_timeout with the job id.
    sync_timeout_seconds: float = 1800
    segmenting: SegmentingConfig = field(default_factory=SegmentingConfig)


@dataclass
class ModelEntry:
    id: str
    adapter: str
    description: str
    options: dict[str, Any]


@dataclass
class VoiceEntry:
    name: str
    description: str
    language: Optional[str]
    reference_audio: Path
    models: list[str]
    transcript: Optional[str] = None


@dataclass
class Registry:
    default_model: str
    models: dict[str, ModelEntry]
    voices: dict[str, VoiceEntry]

    def resolve_model(self, model_id: Optional[str]) -> ModelEntry:
        if model_id is None:
            return self.models[self.default_model]
        if model_id not in self.models:
            raise TTSError("unknown_model", f"Unknown model '{model_id}'.", 404,
                           available=sorted(self.models))
        return self.models[model_id]

    def resolve_voice(self, name: Optional[str], model_id: str) -> Optional[VoiceEntry]:
        if name is None:
            return None
        compatible = sorted(v.name for v in self.voices.values() if model_id in v.models)
        if name not in self.voices:
            raise TTSError("unknown_voice", f"Unknown voice '{name}'.", 404, available=compatible)
        voice = self.voices[name]
        if model_id not in voice.models:
            raise TTSError("incompatible_voice", f"Voice '{name}' does not support model '{model_id}'.",
                           400, available=compatible)
        return voice


def _read_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_server_config() -> ServerConfig:
    raw = _read_yaml("server.yaml")
    seg = SegmentingConfig(**raw.pop("segmenting", {}))
    cfg = ServerConfig(**raw, segmenting=seg)
    if "LOCAL_TTS_PORT" in os.environ:
        cfg.port = int(os.environ["LOCAL_TTS_PORT"])
    return cfg


def load_registry() -> Registry:
    raw_models = _read_yaml("models.yaml")
    models = {}
    for model_id, m in raw_models.get("models", {}).items():
        m = dict(m)
        models[model_id] = ModelEntry(id=model_id, adapter=m.pop("adapter"),
                                      description=m.pop("description", ""), options=m)
    default = raw_models.get("default")
    if default not in models:
        raise ValueError(f"models.yaml default '{default}' is not a registered model")

    voices = {}
    for name, v in _read_yaml("voices.yaml").get("voices", {}).items():
        ref = Path(v["reference_audio"])
        if not ref.is_absolute():
            ref = PROJECT_ROOT / ref
        if not ref.exists():
            raise ValueError(f"voice '{name}': reference_audio not found: {ref}")
        unknown = set(v.get("models", [])) - set(models)
        if unknown:
            raise ValueError(f"voice '{name}': unknown models {sorted(unknown)}")
        voices[name] = VoiceEntry(name=name, description=v.get("description", ""),
                                  language=v.get("language"), reference_audio=ref,
                                  models=list(v.get("models", [])), transcript=v.get("transcript"))
    return Registry(default_model=default, models=models, voices=voices)
