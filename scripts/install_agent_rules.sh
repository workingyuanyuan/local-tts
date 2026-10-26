#!/bin/bash
# Install the narration rules: Claude Code skill + a marked block in ~/.codex/AGENTS.md.
# Idempotent: re-running replaces the skill and the block between the markers; the rest of
# AGENTS.md is kept. Refuses to touch AGENTS.md when the markers are not exactly zero or one
# well-ordered pair. The first run backs up an existing AGENTS.md to AGENTS.md.bak-local-tts.
# {{LOCAL_TTS_ROOT}} in the rule sources becomes this checkout's absolute path.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)

SKILL_DIR=$HOME/.claude/skills/narration
mkdir -p "$SKILL_DIR"
render() { python3 -c 'import sys; print(open(sys.argv[1], encoding="utf-8").read().replace("{{LOCAL_TTS_ROOT}}", sys.argv[2]), end="")' "$1" "$ROOT"; }
render "$ROOT/agent-rules/claude/narration/SKILL.md" > "$SKILL_DIR/.SKILL.md.new"
chmod 644 "$SKILL_DIR/.SKILL.md.new" && mv "$SKILL_DIR/.SKILL.md.new" "$SKILL_DIR/SKILL.md"
echo "installed $SKILL_DIR/SKILL.md"

AGENTS=$HOME/.codex/AGENTS.md
mkdir -p "$(dirname "$AGENTS")"
if [ -e "$AGENTS" ] && [ ! -e "$AGENTS.bak-local-tts" ]; then cp -p "$AGENTS" "$AGENTS.bak-local-tts"; fi
python3 - "$AGENTS" "$ROOT/agent-rules/codex/AGENTS.local-tts.md" "$ROOT" <<'PY'
import os, sys, tempfile
path, block_path, root = sys.argv[1], sys.argv[2], sys.argv[3]
BEGIN, END = "<!-- local-tts:begin -->", "<!-- local-tts:end -->"
text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
rules = open(block_path, encoding="utf-8").read().strip().replace("{{LOCAL_TTS_ROOT}}", root)
block = f"{BEGIN}\n{rules}\n{END}\n"
nb, ne = text.count(BEGIN), text.count(END)
if (nb, ne) == (0, 0):
    new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
elif (nb, ne) == (1, 1) and text.index(BEGIN) < text.index(END):
    start, end = text.index(BEGIN), text.index(END) + len(END)
    new = text[:start] + block.rstrip("\n") + text[end:]
else:
    sys.exit(f"{path}: expected zero or one ordered pair of local-tts markers, found {nb} begin / {ne} end; "
             "fix the file by hand (nothing was changed)")
mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".AGENTS.md.")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.chmod(tmp, mode)
    os.replace(tmp, path)  # atomic: readers see the old or the new file, never a partial one
except BaseException:
    os.unlink(tmp)
    raise
PY
echo "updated $AGENTS (block between local-tts markers)"
