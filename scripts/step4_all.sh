#!/bin/bash
# Run the whole step-4 acceptance:
#   1. Claude Code and Codex positive tasks at the same time (two step4_run.sh processes, so their TTS
#      requests share the GPU queue);
#   2. then both negative tasks, one after the other with nothing else running, so the service log
#      can prove that they queued no job.
# Exit codes go to <out_dir>/exit_codes.txt.
# Usage: scripts/step4_all.sh <work_dir> <out_dir>
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(realpath -m "$1"); OUT=$(realpath -m "$2")
mkdir -p "$OUT"
"$ROOT/scripts/step4_run.sh" "$WORK/claude" "$OUT/claude" claude_video claude_reading > "$OUT/claude.run.log" 2>&1 & C=$!
"$ROOT/scripts/step4_run.sh" "$WORK/codex" "$OUT/codex" codex_video codex_reading > "$OUT/codex.run.log" 2>&1 & X=$!
wait $C; RC_C=$?
wait $X; RC_X=$?
"$ROOT/scripts/step4_run.sh" "$WORK/negatives" "$OUT/negatives" claude_negative codex_negative > "$OUT/negatives.run.log" 2>&1
RC_N=$?
printf 'claude %s\ncodex %s\nnegatives %s\n' $RC_C $RC_X $RC_N | tee "$OUT/exit_codes.txt"
cat "$OUT/claude.run.log" "$OUT/codex.run.log" "$OUT/negatives.run.log"
[ $RC_C = 0 ] && [ $RC_X = 0 ] && [ $RC_N = 0 ]
