#!/bin/bash
# Full step-2 verification run. Keeps every raw record under the output directory.
# Stops the systemd service for the duration (one GPU) and restarts it afterwards.
# Usage: scripts/step2_run.sh <out_dir>
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(realpath -m "$1")
mkdir -p "$OUT/config" "$OUT/config_nochain" "$OUT/config_trunc"
cd "$ROOT"
export PATH=$HOME/.local/bin:$PATH HF_HUB_OFFLINE=1 TQDM_DISABLE=1 HF_HUB_DISABLE_PROGRESS_BARS=1 PYTHONUNBUFFERED=1

SERVER_PID=""
stop_server() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null && wait "$SERVER_PID" 2>/dev/null
  SERVER_PID=""
}
cleanup() {
  stop_server
  [ -n "${SMI:-}" ] && kill "$SMI" 2>/dev/null
  systemctl --user start local-tts
}
trap cleanup EXIT

start_server() {  # <config_dir> <port> <log>
  LOCAL_TTS_CONFIG_DIR=$1 .venv/bin/tts-server > "$3" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 60); do curl -sf "localhost:$2/health" >/dev/null && return 0; sleep 1; done
  echo "server on $2 did not start"; exit 1
}

for c in "$OUT/config" "$OUT/config_nochain" "$OUT/config_trunc"; do cp config/models.yaml config/voices.yaml "$c/"; done
# Truncation config: a 150-frame (~12 s) cap forces the split-and-retry path on ~20 s segments.
sed -i 's/^    max_new_tokens: .*/    max_new_tokens: 150/' "$OUT/config_trunc/models.yaml"
sed -e 's/^port: .*/port: 8766/' -e 's/^queue_max: .*/queue_max: 3/' -e 's/^idle_unload_seconds: .*/idle_unload_seconds: 60/' \
  config/server.yaml > "$OUT/config/server.yaml"
sed -e 's/^port: 8766/port: 8767/' -e 's/unvoiced_consistency: .*/unvoiced_consistency: none/' \
  "$OUT/config/server.yaml" > "$OUT/config_nochain/server.yaml"
sed -e 's/^port: 8766/port: 8768/' "$OUT/config/server.yaml" > "$OUT/config_trunc/server.yaml"

systemctl --user stop local-tts
nvidia-smi --query-gpu=timestamp,memory.used --format=csv,noheader,nounits -lms 500 > "$OUT/nvidia-smi.csv" &
SMI=$!

echo "== server (queue_max=3, idle_unload=60s)"
start_server "$OUT/config" 8766 "$OUT/server.log"
B=http://127.0.0.1:8766
echo "== concurrency"
.venv/bin/python scripts/step2_concurrency.py $B "$OUT" || exit 1
echo "== long texts"
.venv/bin/python scripts/step2_long.py $B "$OUT" || exit 1
echo "== idle unload"
for _ in $(seq 1 180); do grep -q "unloaded moss" "$OUT/server.log" && break; sleep 1; done
grep -q "unloaded moss" "$OUT/server.log" || { echo "no idle unload"; exit 1; }
curl -s $B/health > "$OUT/health_after_unload.json"
CODE=$(curl -s -w '%{http_code}' -D "$OUT/after_unload.headers" -o "$OUT/after_unload.wav" $B/v1/audio/speech \
  -H 'Content-Type: application/json' -d '{"input":"閒置卸載之後，再次請求仍然可以正常生成。","language":"zh","voice":"demo_zh_female"}')
[ "$CODE" = 200 ] || { echo "request after unload failed: HTTP $CODE"; exit 1; }
curl -s $B/health > "$OUT/health_after_reload.json"
stop_server

echo "== no-chain comparison server"
start_server "$OUT/config_nochain" 8767 "$OUT/server_nochain.log"
LONG_SUFFIX=_nochain .venv/bin/python scripts/step2_long.py http://127.0.0.1:8767 "$OUT" long_zh_novoice long_en_novoice || exit 1
stop_server

echo "== forced truncation (max_new_tokens=150)"
start_server "$OUT/config_trunc" 8768 "$OUT/server_trunc.log"
TRUNC_TEXT='第一段：本地語音合成服務會把長篇文字依照句子的邊界切成多個段落，並且在每一段生成之後檢查是否被截斷。如果某一段達到生成上限，系統會把它切得更細，然後重新生成，直到每一小段都能完整地念完為止。[pause 2s]這樣就能避免漏掉句子。'
.venv/bin/python - "$OUT" "$TRUNC_TEXT" <<'PY' || exit 1
import json, sys, httpx
out, text = sys.argv[1], sys.argv[2]
r = httpx.post("http://127.0.0.1:8768/v1/audio/speech", json={"input": text, "language": "zh", "seed": 3}, timeout=900)
info = {"http": r.status_code, "headers": {k: v for k, v in r.headers.items() if k.startswith("x-tts")}}
if r.status_code == 200:
    open(f"{out}/truncation_split.wav", "wb").write(r.content)
else:
    info["body"] = r.json()
json.dump(info, open(f"{out}/truncation.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(info, ensure_ascii=False))
splits = int(info["headers"].get("x-tts-splits", 0))
if r.status_code != 200 or splits < 1:
    sys.exit(f"forced truncation did not split and succeed (http={r.status_code}, splits={splits})")
PY
grep -E "truncated|splitting" "$OUT/server_trunc.log" | head -5
printf '%s' "$TRUNC_TEXT" > "$OUT/truncation_text.txt"
stop_server
kill $SMI; SMI=""

scripts/step2_eval.sh "$OUT" || exit 1
echo "done: $OUT"
