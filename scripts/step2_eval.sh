#!/bin/bash
# Evaluate a step-2 run directory: ASR round-trip, speaker consistency, ffprobe/decode check.
# Usage: scripts/step2_eval.sh <out_dir>   (called by step2_run.sh)
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(realpath -m "$1")
cd "$ROOT"
export PATH=$HOME/.local/bin:$PATH
echo "== ASR"
# Evaluation models are pinned by revision; allow the hub so they can be fetched if missing.
unset HF_HUB_OFFLINE
A="uvx -q --python 3.12 --with faster-whisper==1.2.1 --with zhconv==1.4.3 python -I scripts/asr_compare.py"
$A tests/data/long_zh.txt "$OUT"/long_zh_{novoice,voice,novoice_nochain}.wav > "$OUT/asr_zh.jsonl" 2> "$OUT/asr_zh.log" || exit 1
$A tests/data/long_en.txt "$OUT"/long_en_{voice,novoice,novoice_nochain}.wav > "$OUT/asr_en.jsonl" 2> "$OUT/asr_en.log" || exit 1
$A "$OUT/truncation_text.txt" "$OUT/truncation_split.wav" > "$OUT/asr_truncation.jsonl" 2> "$OUT/asr_truncation.log" || exit 1
echo "== speaker consistency"
uvx -q --python 3.12 --index https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match \
  --with torch==2.9.1 --with torchaudio==2.9.1 --with speechbrain==1.1.2 \
  python -I scripts/speaker_consistency.py "$OUT"/long_*.wav > "$OUT/speaker_consistency.jsonl" 2> "$OUT/speaker_consistency.log" || exit 1
echo "== ffprobe / full decode"
FAILED=0
: > "$OUT/ffprobe.txt"
for f in "$OUT"/*.wav "$OUT"/*.flac "$OUT"/*.mp3; do
  [ -e "$f" ] || continue
  if info=$(ffprobe -v error -show_entries stream=codec_name,sample_rate,channels:format=duration -of csv=p=0 "$f") \
     && err=$(ffmpeg -v error -i "$f" -f null - 2>&1) && [ -z "$err" ]; then
    echo "$(basename "$f") $(echo "$info" | tr '\n' ' ') decode=ok" >> "$OUT/ffprobe.txt"
  else
    echo "$(basename "$f") decode=FAIL" >> "$OUT/ffprobe.txt"; FAILED=1
  fi
done
[ $FAILED -eq 0 ] || { echo "some outputs failed to decode"; exit 1; }
