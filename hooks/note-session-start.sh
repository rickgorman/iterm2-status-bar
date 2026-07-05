#!/usr/bin/env bash
# Claude Code SessionStart hook — re-emit stored note on resume (see lib/notebar.py).
[ -n "$ITERM_NOTE_HOOK" ] && exit 0
[ -z "$ITERM_SESSION_ID" ] && exit 0
DIR="$(cd "$(dirname "$0")/.." && pwd)"
input=$(cat)
( printf '%s' "$input" | python3 "$DIR/lib/notebar.py" session-start >/dev/null 2>&1 & )
exit 0
