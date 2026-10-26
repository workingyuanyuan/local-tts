#!/bin/bash
# Step-4 acceptance: Claude Code and Codex get real tasks (headless) that never mention the TTS tool.
# Positive tasks (narrated video, teaching material to listen to) must make them call local-tts and
# produce usable media; the negative task (script text only) must not reach the service at all.
# Negative tasks also check the service log for queued jobs, so run them when nothing else uses the
# service (step4_all.sh runs them last, one at a time).
# Assertions live in scripts/step4_check.py; any parse or checker failure is a FAIL.
# Usage: scripts/step4_run.sh <work_dir> <out_dir> [task...]
#   work_dir  where the agents work: a new or empty directory (or one this script created before),
#             outside this repo (its AGENTS.md / CLAUDE.md would leak context). Task subdirectories are recreated.
#   out_dir   logs, evidence, assertions and copies of the products
#   task      subset of: claude_video claude_reading claude_negative codex_video codex_reading codex_negative
# Everything, both agents at once (also exercises the GPU queue): scripts/step4_all.sh
# STEP4_RECHECK=1 re-runs only the checks on an earlier run (same work_dir/out_dir, uses <task>.run).
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ALL="claude_video claude_reading claude_negative codex_video codex_reading codex_negative"
WORK=$(realpath -m "$1"); OUT=$(realpath -m "$2"); shift 2
TASKS=${*:-$ALL}
for t in $TASKS; do [[ " $ALL " == *" $t "* ]] || { echo "unknown task: $t" >&2; exit 2; }; done
case "$WORK/" in "$ROOT"/*) echo "work_dir must be outside $ROOT" >&2; exit 2;; esac
MARK="$WORK/.local-tts-step4"
if [ -e "$WORK" ] && [ ! -e "$MARK" ] && [ -n "$(ls -A "$WORK")" ]; then
  echo "$WORK is not empty and was not created by this script; refusing to delete anything in it" >&2; exit 2
fi
mkdir -p "$WORK" "$OUT" && touch "$MARK"
FAIL=0
record() { echo "$1 ${*:2}" | tee -a "$OUT/summary.txt"; [ "$1" = PASS ] || FAIL=1; }
check_lines() {  # <checker output> — every line must be PASS/FAIL; anything else (or nothing) fails
  [ -n "$1" ] || { record FAIL "checker printed nothing"; return; }
  while IFS= read -r line; do
    case $line in PASS\ *|FAIL\ *) record "${line%% *}" "${line#* }";; INFO\ *) echo "$line" | tee -a "$OUT/summary.txt";;
      *) record FAIL "checker: $line";; esac
  done <<< "$1"
}

prompt() {
  case $1 in
    claude_video) echo '請在目前目錄製作一支 20～30 秒的短片 intro.mp4，主題是「番茄工作法：25 分鐘專注、5 分鐘休息」。分 3 個畫面（純色背景加簡短英文標題即可，可以用 ffmpeg 產生），要有中文旁白，每個畫面的長度要和該段旁白對齊。完成後告訴我成品路徑和長度。';;
    codex_video) echo 'Create a 20-30 second explainer video explainer.mp4 in the current directory about the 3-2-1 backup rule. Use 3 simple title-card scenes (solid color with a short title; ffmpeg is fine) and English narration, with each scene lasting as long as its part of the narration. Tell me the output path and duration when done.';;
    claude_reading) echo 'lesson.md 是給學生的英文教材。請把它做成學生通勤時可以聽的版本，放在這個目錄。';;
    codex_reading) echo '請把 lesson.md 做成學生可以用耳朵聽的版本，放在這個目錄裡。';;
    claude_negative) echo '幫我寫一段 30 秒的產品介紹影片旁白稿，主題是一款保溫杯，存成 script.md。只要文字稿就好，配音我會請配音員錄。';;
    codex_negative) echo 'Write a 30-second voice-over script for a product video about a smart water bottle and save it as script.md. Text only: a professional voice actor will record it.';;
  esac
}

setup() {  # <task> <dir>   (<task> is validated above, so <dir> is always inside $WORK)
  rm -rf "$2"; mkdir -p "$2"
  case $1 in
    claude_reading) cat > "$2/lesson.md" <<'EOF'
# Photosynthesis

Plants make their own food through photosynthesis. Inside the leaves, tiny structures called
chloroplasts capture energy from sunlight. The plant takes in carbon dioxide from the air through
small openings called stomata, and draws water up from the soil through its roots.

Using the energy of light, the plant turns carbon dioxide and water into glucose, a kind of sugar.
Oxygen is released as a by-product, and that oxygen is what we breathe.

## Key points

1. Photosynthesis needs sunlight, water and carbon dioxide.
2. It produces glucose and oxygen.
3. It happens mainly in the leaves, inside the chloroplasts.
EOF
    ;;
    codex_reading) cat > "$2/lesson.md" <<'EOF'
# 水循環

地球上的水不斷在海洋、空氣和陸地之間移動，這個過程叫做水循環。

太陽把海洋、湖泊表面的水加熱，水變成水蒸氣升到空中，這叫做蒸發。植物葉子也會把水分散發到空氣中，叫做蒸散作用。

水蒸氣升到高空後遇冷，凝結成小水滴，聚集成雲。當雲裡的水滴越來越大、越來越重，就會以雨、雪或冰雹的形式落下，這叫做降水。

落到地面的水，一部分流進河川、最後回到海洋；一部分滲入地下，成為地下水。然後，水又會再次蒸發，循環不停。

## 重點整理

1. 水循環的四個主要過程：蒸發、凝結、降水、逕流。
2. 推動水循環的能量來自太陽。
3. 水循環讓地球上的淡水不斷更新。
EOF
    ;;
  esac
}

run_agent() {  # <task> <dir> -> transcript in $OUT/<task>.jsonl, exit code in $RC
  local t=$1 d=$2
  if [[ $t == claude_* ]]; then
    # Bash/edits allowed for the task itself; local-tts is allowed by ~/.claude/settings.json.
    (cd "$d" && timeout 1200 claude -p "$(prompt "$t")" --permission-mode acceptEdits --allowedTools Bash Skill \
       --output-format stream-json --verbose < /dev/null > "$OUT/$t.jsonl" 2> "$OUT/$t.err")
  else
    # local-tts tools are approved by default_tools_approval_mode in ~/.codex/config.toml.
    (cd "$d" && timeout 1200 codex exec --strict-config --skip-git-repo-check -s workspace-write -C "$d" --json \
       "$(prompt "$t")" < /dev/null > "$OUT/$t.jsonl" 2> "$OUT/$t.err")
  fi
  RC=$?
}

: > "$OUT/summary.txt"; : > "$OUT/asr_pairs.tsv"; : > "$OUT/asr_info.tsv"; : > "$OUT/asr.err"
for t in $TASKS; do
  d="$WORK/$t"
  if [ "${STEP4_RECHECK:-}" = 1 ]; then  # re-run the checks on an earlier run's transcripts and products
    read -r start end RC < "$OUT/$t.run" || { record FAIL "$t: no $t.run to recheck"; continue; }
  else
    setup "$t" "$d"
    rm -f "$OUT/$t.negative.json"  # a fresh run records fresh evidence
    start=$(date +%s)
    run_agent "$t" "$d"
    end=$(date +%s)
    echo "$start $end $RC" > "$OUT/$t.run"
  fi
  secs=$(( end - start ))
  if ! python3 "$ROOT/scripts/step4_evidence.py" "${t%%_*}" "$OUT/$t.jsonl" > "$OUT/$t.evidence.json" 2>> "$OUT/$t.err"; then
    record FAIL "$t: transcript could not be parsed (exit $RC, see $t.err)"; continue
  fi
  # copy the products (not the agents' intermediate frames) for the record; the checker verifies hashes
  mkdir -p "$OUT/$t"; (cd "$d" && find . -maxdepth 3 -type f \( -name '*.wav' -o -name '*.mp3' -o -name '*.flac' \
    -o -name '*.mp4' -o -name '*.md' -o -name '*.txt' \) -exec cp --parents -t "$OUT/$t" {} +)
  check_lines "$("$ROOT/.venv/bin/python" "$ROOT/scripts/step4_check.py" task "$t" "$d" "$OUT/$t.evidence.json" "$RC" "$secs" "$start" "$end" "$OUT")"
done

asr() { uvx -q --python 3.12 --with faster-whisper==1.2.1 --with zhconv==1.4.3 python -I "$ROOT/scripts/asr_compare.py" "$@"; }
if [ -s "$OUT/asr_pairs.tsv" ]; then
  echo "== ASR of every clip vs the text the agent sent (asr.jsonl)" | tee -a "$OUT/summary.txt"
  : > "$OUT/asr.jsonl"
  while IFS=$'\t' read -r src audio; do
    # uvx may fail to reach the package index (seen: a TLS error); the cached environment works offline
    r=$(asr "$src" "$audio" 2>> "$OUT/asr.err" < /dev/null || UV_OFFLINE=1 asr "$src" "$audio" 2>> "$OUT/asr.err" < /dev/null) \
      && echo "$r" >> "$OUT/asr.jsonl" || record FAIL "asr_compare failed on $audio"
  done < "$OUT/asr_pairs.tsv"
  # unresolved ASR candidates fail unless someone listened and recorded it in $OUT/asr_review.tsv
  check_lines "$("$ROOT/.venv/bin/python" "$ROOT/scripts/step4_check.py" asr "$OUT/asr.jsonl" "$OUT/asr_pairs.tsv" "$OUT/asr_review.tsv")"
fi
if [ -s "$OUT/asr_info.tsv" ]; then
  echo "== for reference: ASR of the final videos against all clip texts (asr_info.jsonl)" | tee -a "$OUT/summary.txt"
  : > "$OUT/asr_info.jsonl"
  while IFS=$'\t' read -r src audio; do
    r=$(asr "$src" "$audio" 2>> "$OUT/asr.err" < /dev/null || UV_OFFLINE=1 asr "$src" "$audio" 2>> "$OUT/asr.err" < /dev/null) \
      && echo "$r" >> "$OUT/asr_info.jsonl"
  done < "$OUT/asr_info.tsv"
  check_lines "$("$ROOT/.venv/bin/python" "$ROOT/scripts/step4_check.py" asr-info "$OUT/asr_info.jsonl")"
fi
[ $FAIL = 0 ] && echo "ALL PASS" | tee -a "$OUT/summary.txt"
exit $FAIL
