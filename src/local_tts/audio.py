"""Audio helpers: edge-silence trimming, concatenation and encoding (float32 [channels, samples])."""
import io
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from local_tts.errors import TTSError

FORMATS = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}


def trim_silence(audio: np.ndarray, sr: int, threshold_db: float = -50.0, keep_s: float = 0.08) -> np.ndarray:
    """Trim leading/trailing silence, keeping `keep_s` of margin on each side."""
    frame = max(1, int(sr * 0.01))
    mono = np.abs(audio).max(axis=0)
    n = len(mono) // frame
    if n == 0:
        return audio
    level = mono[: n * frame].reshape(n, frame).max(axis=1)
    loud = np.nonzero(level > 10 ** (threshold_db / 20))[0]
    if loud.size == 0:
        return audio[:, :0]
    keep = int(sr * keep_s)
    start = max(0, loud[0] * frame - keep)
    end = min(audio.shape[1], (loud[-1] + 1) * frame + keep)
    return audio[:, start:end]


def silence(channels: int, sr: int, seconds: float) -> np.ndarray:
    return np.zeros((channels, int(round(sr * max(0.0, seconds)))), dtype=np.float32)


def to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).round().astype("<i2").T.tobytes()


def encode(audio: np.ndarray, sr: int, fmt: str) -> bytes:
    if fmt not in FORMATS:
        raise TTSError("unsupported_format", f"Unsupported response_format '{fmt}'.", 400,
                       available=sorted(FORMATS))
    channels = audio.shape[0]
    if fmt == "wav":
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(to_pcm16(audio))
        return buf.getvalue()
    args = ["-c:a", "flac"] if fmt == "flac" else ["-c:a", "libmp3lame", "-b:a", "192k"]
    # Encode to a seekable file: piped output leaves FLAC STREAMINFO / MP3 Xing headers without
    # the total length, so readers report no duration.
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / f"out.{fmt}"
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(sr), "-ac", str(channels), "-i", "-",
             *args, str(out)],
            input=to_pcm16(audio), capture_output=True,
        )
        if proc.returncode != 0:
            raise TTSError("encode_failed", f"ffmpeg failed to encode {fmt}: {proc.stderr.decode()[-500:]}", 500)
        return out.read_bytes()
