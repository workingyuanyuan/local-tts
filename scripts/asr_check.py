"""ASR round-trip check: transcribe WAVs with faster-whisper and print one line per file.

Usage: uvx --python 3.12 --with faster-whisper==1.2.1 python -I scripts/asr_check.py <wav>... > asr.jsonl
Audio is decoded with ffmpeg (newer PyAV breaks faster-whisper's own decoder).
"""
import json
import subprocess
import sys

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.utils import download_model

m = WhisperModel(download_model("mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                                revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"),
                 device="cpu", compute_type="int8")
for p in sys.argv[1:]:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    segs, info = m.transcribe(audio, beam_size=5)
    text = "".join(s.text for s in segs).strip()
    print(json.dumps(dict(file=p, language=info.language, text=text), ensure_ascii=False), flush=True)
