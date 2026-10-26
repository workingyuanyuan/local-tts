"""Speaker consistency within one file: cosine similarity of ECAPA embeddings of 6 s windows vs. the first window.

Usage:
  uvx --python 3.12 --index https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match \
    --with torch==2.9.1 --with torchaudio==2.9.1 --with speechbrain==1.1.2 \
    python -I scripts/speaker_consistency.py <wav>...
Prints one JSON line per file: mean / min similarity of later windows to the opening window
(higher = more consistent; same speaker is typically > 0.6). Only full 6 s windows are scored and
only against the opening, so this measures drift from the opening voice, not every segment pair.
"""
import json
import subprocess
import sys

import numpy as np
import torch
from huggingface_hub import snapshot_download
from speechbrain.inference.speaker import EncoderClassifier

SR = 16000
WIN = 6 * SR

ECAPA_REV = "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
enc = EncoderClassifier.from_hparams(source=snapshot_download("speechbrain/spkrec-ecapa-voxceleb", revision=ECAPA_REV))
for path in sys.argv[1:]:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    wav = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    windows = [wav[i:i + WIN] for i in range(0, len(wav) - WIN + 1, WIN)]
    windows = [w for w in windows if np.sqrt(np.mean(w ** 2)) > 0.01]  # skip mostly-silent windows
    with torch.no_grad():
        emb = enc.encode_batch(torch.from_numpy(np.stack(windows))).squeeze(1)
    emb = torch.nn.functional.normalize(emb, dim=-1)
    sims = (emb[1:] @ emb[0]).tolist()
    print(json.dumps({"file": path, "windows": len(windows), "mean_vs_first": round(float(np.mean(sims)), 3),
                      "min_vs_first": round(float(np.min(sims)), 3),
                      "per_window": [round(s, 2) for s in sims]}), flush=True)
