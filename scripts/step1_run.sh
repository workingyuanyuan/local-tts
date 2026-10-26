#!/bin/bash
# Re-run step 1 for one codec dtype and keep all raw logs under outputs/step1/<dtype>/.
# Usage: scripts/step1_run.sh {bf16|fp32}
set -u
DT=$1
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$ROOT/outputs/step1/$DT
mkdir -p "$OUT"
cd "$ROOT"
nvidia-smi --query-gpu=timestamp,memory.used --format=csv,noheader,nounits -lms 500 > "$OUT/nvidia-smi.csv" &
SMI=$!
trap 'kill $SMI 2>/dev/null' EXIT
HF_HUB_OFFLINE=1 TQDM_DISABLE=1 HF_HUB_DISABLE_PROGRESS_BARS=1 \
  .venv/bin/python scripts/step1_smoke.py "$OUT" assets/ref/reference_zh.wav --codec-dtype "$DT" --repeat \
  > "$OUT/smoke.log" 2>&1
RC=$?
kill $SMI 2>/dev/null
echo "smoke exit=$RC"
grep -E "^\[(CODEC|MEM|LOAD|RESULT)\]" "$OUT/smoke.log"
echo "nvidia-smi whole-GPU max sample (500 ms) MiB: $(cut -d, -f2 "$OUT/nvidia-smi.csv" | sort -n | tail -1)"
[ $RC -eq 0 ] || exit $RC
PATH=$HOME/.local/bin:$PATH uvx -q --python 3.12 --with faster-whisper==1.2.1 \
  python -I scripts/asr_check.py "$OUT"/*.wav > "$OUT/asr.jsonl" 2> "$OUT/asr.log" || exit $?
cat "$OUT/asr.jsonl"
