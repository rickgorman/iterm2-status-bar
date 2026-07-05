# AGENT.md

Orientation for AI agents working in this repo. Read README.md first for the user-facing picture; this file covers layout, invariants, and the sharp edges discovered while building it.

## What this is

Claude Code hooks → `lib/notebar.py` → `claude -p --model haiku` → iTerm2 `user.note` variable → status bar. One ~15-word line per session: a per-turn THRUST (`state · next: step`). A sticky 2–4 word GOAL label is tracked in state (not displayed) to anchor generation and pivot detection.

## Layout

```
hooks/
  note-prompt.sh         UserPromptSubmit wrapper
  note-stop.sh           Stop wrapper
  note-session-start.sh  SessionStart wrapper
lib/
  notebar.py             ALL logic lives here (handlers, state, lock, claude call, iTerm2 API)
setup/
  add-statusbar-component.py  one-time: adds Interpolated String component to the profile
```

The bash hooks are deliberately dumb: guard-check, capture stdin, background `notebar.py`, exit 0. Behavior changes belong in `notebar.py`, not the wrappers.

Runtime state (not in repo): `~/.claude/statusbar/<session_id>.json`, log at `~/.claude/statusbar/notebar.log`. Install wiring: `~/.claude/settings.json` hook entries.

## Invariants — do not break

1. **Hooks must return instantly.** All LLM/API work runs in a detached background subshell. Never make the wrapper wait on `notebar.py`.
2. **Recursion guard**: nested `claude -p` runs with `ITERM_NOTE_HOOK=1`; every entry point exits early when set. Users' other hooks (notification sounds, bells, loggers) may check the same variable to stay silent — renaming it breaks that contract.
3. **Never feed the whole transcript.** Stop updates read only a tail slice (`tail_lines`, 256 KB cap) and send four small strings to Haiku. Transcripts reach hundreds of MB.
4. **Word budget enforced in code**, not just prompt: `clamp_words`, goal capped at `PREFIX_MAX_WORDS`. Model output is untrusted — `gen_update` parses `GOAL:`/`THRUST:` lines defensively and keeps the old goal on any parse failure.
5. **Per-session flock** around Stop generation; a busy lock skips (logs `locked, skipping`), never queues.
6. **Fail silent.** Hooks must never disturb the user's session: no stdout/stderr noise, exceptions logged to `notebar.log` and swallowed.

## Testing recipes

Drive handlers directly with fake hook JSON (no live session needed):

```sh
# first-prompt → goal generation
printf '{"session_id":"t1","prompt":"add dark mode toggle"}' \
  | python3 lib/notebar.py prompt

# turn-complete → status update (any real transcript works)
printf '{"session_id":"t1","transcript_path":"'$HOME'/.claude/projects/<proj>/<sid>.jsonl"}' \
  | python3 lib/notebar.py stop

python3 -m json.tool ~/.claude/statusbar/t1.json
tail ~/.claude/statusbar/notebar.log
rm -f ~/.claude/statusbar/t1.json*        # clean up test state
```

Guard check: prepend `ITERM_NOTE_HOOK=1` — must exit instantly with no state file written.

Verify display without pixels: read the variable back —
```python
note = await session.async_get_variable("user.note")
```

## Gotchas (hard-won)

- **CG window owner name is `iTerm`, not `iTerm2`** when enumerating windows via CGWindowList.
- **`screencapture -l <id>` of a window on an inactive Space returns a blank image**; AppleScript reports 0 windows when iTerm is fullscreen on another Space. Screenshot verification only works when iTerm is frontmost — poll `System Events` frontmost process and capture then.
- **Claude Code snapshots hooks at session start.** Editing `settings.json` does nothing for running sessions; test by driving `notebar.py` directly (above) or start a fresh session.
- **iTerm2 Python API has no public setter for the status bar layout dict** — `setup/add-statusbar-component.py` uses `Profile._async_simple_set("Status Bar Layout", ...)`, the same call the generated setters wrap. May need attention on iterm2 package upgrades.
- The Interpolated String expression uses `\(user.note?)` — the `?` renders empty (instead of erroring) when the variable is unset.
- **Haiku occasionally returns junk or echoes the request** — that's why goal fallback, prefix clamping, and empty-output logging exist. Don't assume a single clean-label response.
- `swift -e` one-liners can fail with a CommandLineTools `SwiftBridging` module clash; `clang` + ObjC is the reliable route for CoreGraphics probing.

## Style

- Python: stdlib only (plus `iterm2` imported lazily inside `set_note`). Keep it one file unless it genuinely outgrows that.
- Log via `log()` — timestamped lines in `notebar.log`. No print() in hook paths.
- Prompts to Haiku live inline in `gen_prefix` / `gen_update`; keep the hard rules (budget, stickiness, output format) explicit in the prompt AND enforced in code.
