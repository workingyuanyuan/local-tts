#!/bin/bash
# Install tts-server as a systemd user service for this checkout (any location without spaces, quotes,
# backslashes or %, which systemd unit syntax would mangle).
# Renders deploy/local-tts.service ({{LOCAL_TTS_ROOT}} -> this checkout) into
# ~/.config/systemd/user/, reloads systemd, enables the service and (re)starts it on the current code.
# Without systemd, run `.venv/bin/tts-server` in the foreground instead.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=$ROOT/.venv/bin/python
case $ROOT in *[[:space:]%\\\"\']*) echo "unsupported characters in '$ROOT' (space, %, quote or backslash); move the checkout" >&2; exit 1;; esac
# A moved checkout keeps a .venv whose scripts point at the old location: it must be recreated.
if ! [ -x "$ROOT/.venv/bin/tts-server" ] || [ "$("$PY" -c 'import sys; print(sys.prefix)' 2>/dev/null)" != "$ROOT/.venv" ] \
   || ! head -1 "$ROOT/.venv/bin/tts-server" | grep -qF "$ROOT/.venv/"; then
  echo "$ROOT/.venv is missing or was created elsewhere; run: rm -rf .venv && uv sync --frozen" >&2; exit 1
fi
command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1 \
  || { echo "systemd user manager not available; run '$ROOT/.venv/bin/tts-server' in the foreground instead" >&2; exit 1; }
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$UNIT_DIR"
"$PY" -c 'import sys; print(open(sys.argv[1], encoding="utf-8").read().replace("{{LOCAL_TTS_ROOT}}", sys.argv[2]), end="")' \
  "$ROOT/deploy/local-tts.service" "$ROOT" > "$UNIT_DIR/.local-tts.service.new"
mv "$UNIT_DIR/.local-tts.service.new" "$UNIT_DIR/local-tts.service"
systemctl --user daemon-reload
systemctl --user enable local-tts
systemctl --user restart local-tts  # also when it was already running (e.g. from another checkout)
echo "installed $UNIT_DIR/local-tts.service (WorkingDirectory=$ROOT) and restarted local-tts"
