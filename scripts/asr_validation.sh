#!/bin/bash
# Check that scripts/asr_compare.py catches real defects and does not fail normal speech.
# <dir>/manifest.tsv lists "expect<TAB>source text<TAB>audio<TAB>note" (paths relative to <dir>);
# expect is "fail" (a dropped or extra-speech defect, made on purpose: must stay unresolved), "pass"
# (normal output, including files where Whisper hallucinates but the crop clears it: must be clean) or
# "review" (normal output the automatic check cannot clear: must be flagged for a listener, which
# records the false-alarm rate). Writes results.jsonl, sha256.txt, summary.txt; the previous
# results.jsonl is kept as results.prev.jsonl and compared in comparison.txt.
# Usage: scripts/asr_validation.sh outputs/step4/asr_validation
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIR=$(realpath "$1")
cd "$DIR"
# keep the previous run so that repeated runs can be compared (ASR decoding is deterministic)
[ -s results.jsonl ] && mv results.jsonl results.prev.jsonl
: > results.jsonl; : > summary.txt; : > asr.err
grep -v '^#' manifest.tsv | cut -f2,3 | tr '\t' '\n' | sort -u | xargs sha256sum > sha256.txt
asr() { uvx -q --python 3.12 --with faster-whisper==1.2.1 --with zhconv==1.4.3 python -I "$ROOT/scripts/asr_compare.py" "$@"; }
FAIL=0
while IFS=$'\t' read -r expect src audio note; do
  [[ $expect == \#* ]] && continue
  r=$(asr "$src" "$audio" 2>> asr.err < /dev/null || UV_OFFLINE=1 asr "$src" "$audio" 2>> asr.err < /dev/null)
  if [ -z "$r" ]; then echo "FAIL $audio: asr_compare failed" | tee -a summary.txt; FAIL=1; continue; fi
  echo "$r" >> results.jsonl
  got=$(python3 -c 'import json,sys; print("fail" if json.loads(sys.argv[1])["unresolved"] else "pass")' "$r")
  detail=$(python3 -c 'import json,sys; r=json.loads(sys.argv[1]); print([(c["kind"], c["source"], c["heard"]) for c in r["unresolved"]])' "$r")
  want=$([ "$expect" = pass ] && echo pass || echo fail)
  if [ "$got" = "$want" ]; then v=PASS; else v=FAIL; FAIL=1; fi
  echo "$v $audio: expected $expect, got $got $detail ($note)" | tee -a summary.txt
done < manifest.tsv
if [ -s results.prev.jsonl ]; then
  python3 - <<'PY' | tee comparison.txt
import json
a = [json.loads(l) for l in open("results.prev.jsonl", encoding="utf-8")]
b = [json.loads(l) for l in open("results.jsonl", encoding="utf-8")]
key = lambda r: (r["file"], r["audio_sha256"], r["transcript"], json.dumps(r["unresolved"], ensure_ascii=False))
same = len(a) == len(b) and all(key(x) == key(y) for x, y in zip(a, b))
print(f"previous run vs this run: {len(a)} / {len(b)} results, "
      f"{'identical transcripts and verdicts' if same else 'DIFFERENT'}")
PY
fi
[ $FAIL = 0 ] && echo "ALL PASS" | tee -a summary.txt
exit $FAIL
