#!/bin/bash
# Step-3 verification against the running service: tts CLI + tts-mcp over stdio.
# Every check asserts its outcome; raw output goes to <out_dir>.
# Usage: scripts/step3_run.sh <out_dir> [--agents]
#   --agents  also ask Claude Code and Codex (headless) to call the MCP tool; logs are kept.
#             Codex gets a one-off approval override (-c ...approve); the config is not changed.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(realpath -m "$1")
AGENTS=${2:-}
mkdir -p "$OUT"
cd "$ROOT"
TTS=.venv/bin/tts
FAIL=0
check() {  # <name> <expected exit> <expected error code or -> -- command...
  local name=$1 want=$2 code=$3; shift 4
  "$@" > "$OUT/$name.out" 2> "$OUT/$name.err"
  local got=$?
  local ok=1
  [ "$got" = "$want" ] || ok=0
  if [ "$code" != - ]; then grep -q "\"code\": \"$code\"" "$OUT/$name.err" || ok=0; fi
  echo "$([ $ok = 1 ] && echo PASS || echo FAIL) $name (exit $got, want $want${code:+, $code})" | tee -a "$OUT/summary.txt"
  [ $ok = 1 ] || FAIL=1
}
: > "$OUT/summary.txt"
rm -f "$OUT"/*.wav "$OUT"/*.mp3 "$OUT"/*.flac "$OUT"/agent_* "$OUT"/mcp_stdio.json

check health 0 - -- $TTS health
check say_voice_mp3 0 - -- $TTS say "這是命令列工具的實測，音檔會寫到指定的絕對路徑。" -l zh -v demo_zh_female -o "$OUT/cli_zh.mp3" --json -q
check exists_error 1 output_exists -- $TTS say "again" -o "$OUT/cli_zh.mp3"
check overwrite_first 0 - -- $TTS say "覆寫前。" -l zh -o "$OUT/cli_overwrite.wav" -q
check overwrite 0 - -- $TTS say "覆寫測試。" -l zh -o "$OUT/cli_overwrite.wav" --overwrite -q
check relative_path 1 invalid_path -- $TTS say "relative" -o rel.wav
check bad_ext 1 unsupported_format -- $TTS say "x" -o "$OUT/x.ogg"
check unknown_voice 1 unknown_voice -- $TTS say "x" -v alloy -o "$OUT/never.wav"
check unknown_model 1 unknown_model -- $TTS say "x" -m tts-1 -o "$OUT/never.wav"
check stdin 0 - -- bash -c 'echo "Piped text works too." | "$0" say -l en -o "$1" -q --json' "$TTS" "$OUT/cli_stdin.flac"
check no_wait 3 - -- $TTS say "先送出、稍後再取回音檔。" -l zh -o "$OUT/cli_later.wav" --no-wait --json
JOB=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['job_id'])" "$OUT/no_wait.out")
check job_wait 0 - -- $TTS job "$JOB" --wait -o "$OUT/cli_later.wav" -q --json
if [ -x /mnt/c/Windows/System32/cmd.exe ]; then
  WT=$(/mnt/c/Windows/System32/cmd.exe /c "echo %TEMP%" 2>/dev/null | tr -d '\r')
  check windows_path 0 - -- $TTS say "Windows 路徑測試。" -l zh -o "$WT\\local-tts-step3.wav" -q --json
  python3 -c "import json,os,sys; os.remove(json.load(open(sys.argv[1]))['path'])" "$OUT/windows_path.out"
fi

echo "== MCP over stdio" | tee -a "$OUT/summary.txt"
if .venv/bin/python scripts/step3_mcp_stdio.py "$OUT" > "$OUT/mcp_stdio.log" 2>&1; then
  echo "PASS mcp_stdio" | tee -a "$OUT/summary.txt"
else
  echo "FAIL mcp_stdio (see mcp_stdio.log)" | tee -a "$OUT/summary.txt"; FAIL=1
fi

record() {  # <PASS|FAIL> <message>  (not used in a pipeline, so FAIL=1 sticks)
  echo "$1 $2" | tee -a "$OUT/summary.txt"
  [ "$1" = PASS ] || FAIL=1
}
agent_result() {  # <name> <exit code> <tool-call evidence found 0/1>
  if [ "$2" = 0 ] && [ "$3" = 1 ] && [ -s "$OUT/$1.wav" ]; then record PASS "$1"
  else record FAIL "$1 (exit $2, tool call evidence $3, audio $([ -s "$OUT/$1.wav" ] && echo yes || echo no))"; fi
}
if [ "$AGENTS" = --agents ]; then
  echo "== agents (headless, explicitly asked to use the tool)" | tee -a "$OUT/summary.txt"
  ASK='Use the local-tts MCP tool generate_speech to speak the sentence "%s" (language %s) and write it to %s. Then reply with only the returned JSON.'
  # stream-json keeps every event, including the tool_use and its tool_result.
  (cd "$OUT" && timeout 300 claude -p "$(printf "$ASK" "Claude Code 已經可以呼叫本地語音服務。" zh "$OUT/agent_claude.wav")" \
     --allowedTools mcp__local-tts__generate_speech --output-format stream-json --verbose \
     < /dev/null > "$OUT/agent_claude.jsonl" 2> "$OUT/agent_claude.err")
  RC=$?
  EVIDENCE=$(python3 - "$OUT/agent_claude.jsonl" <<'PY'
import json, sys
uses, ok = set(), False
for line in open(sys.argv[1]):
    ev = json.loads(line)
    for block in (ev.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "mcp__local-tts__generate_speech":
            uses.add(block["id"])
        if block.get("type") == "tool_result" and block.get("tool_use_id") in uses and not block.get("is_error"):
            content = block.get("content")
            ok = ok or '"status":"completed"' in (content if isinstance(content, str) else json.dumps(content)).replace(" ", "")
print(1 if ok else 0)
PY
)
  agent_result agent_claude $RC "$EVIDENCE"
  (cd "$OUT" && timeout 300 codex exec -c 'mcp_servers.local-tts.default_tools_approval_mode="approve"' --skip-git-repo-check -C "$OUT" \
     "$(printf "$ASK" "Codex can now call the local speech service." en "$OUT/agent_codex.wav")" < /dev/null > "$OUT/agent_codex.log" 2>&1)
  RC=$?
  grep -q "mcp: local-tts/generate_speech (completed)" "$OUT/agent_codex.log" && EVIDENCE=1 || EVIDENCE=0
  agent_result agent_codex $RC $EVIDENCE
fi

echo "== outputs: present, 48 kHz stereo, full decode, mode 644" | tee -a "$OUT/summary.txt"
EXPECTED="cli_zh.mp3 cli_overwrite.wav cli_stdin.flac cli_later.wav mcp_short.wav mcp_long.mp3"
[ "$AGENTS" = --agents ] && EXPECTED="$EXPECTED agent_claude.wav agent_codex.wav"
for name in $EXPECTED; do
  f="$OUT/$name"
  if [ ! -s "$f" ]; then record FAIL "$name missing"; continue; fi
  codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$f")
  sr=$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of csv=p=0 "$f")
  ch=$(ffprobe -v error -select_streams a:0 -show_entries stream=channels -of csv=p=0 "$f")
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
  mode=$(stat -c %a "$f")
  err=$(ffmpeg -v error -i "$f" -f null - 2>&1); rc=$?
  if [ "$sr" = 48000 ] && [ "$ch" = 2 ] && [ "$mode" = 644 ] && [ $rc = 0 ] && [ -z "$err" ]; then
    record PASS "$name $codec ${sr}Hz ${ch}ch ${dur}s mode=$mode"
  else
    record FAIL "$name codec=$codec sr=$sr ch=$ch mode=$mode decode_rc=$rc ${err:0:100}"
  fi
done
[ $FAIL = 0 ] && echo "ALL PASS" | tee -a "$OUT/summary.txt"
exit $FAIL
