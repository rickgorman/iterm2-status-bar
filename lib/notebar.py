#!/usr/bin/env python3
"""Maintain a per-session status line in the iTerm2 status bar (user.note).

Driven by Claude Code hooks. Reads the hook event JSON on stdin.

Subcommands:
  prompt         UserPromptSubmit — first prompt generates the frozen prefix;
                 later prompts just record an excerpt and mark the turn active.
  stop           Stop — regenerate the mutable part of the status line from the
                 last assistant message via a haiku `claude -p` call.
  session-start  SessionStart — re-emit the stored status line (resume/new tab).

Status line shape (budget ~15 words total):
  <2-4 word GOAL label>: <current state> · next: <step>

Two facts are tracked:
  GOAL   the high-level thing being built. Sticky: repeated verbatim each
         update unless it clearly misdescribes the session (early sessions
         often open with an exploratory ask before the real goal appears).
  THRUST where work stands right now, plus the obvious next step.

Token strategy: never feed the whole transcript. Inputs per update are the
current goal label, the previous status line, a short excerpt of the latest
user prompt, and the last assistant text message (tail-read from the
transcript).

Recursion guard: the nested `claude -p` runs with ITERM_NOTE_HOOK=1; every hook
in this system (and agent-familiar's hook.sh) exits early when it is set.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser("~/.claude/statusbar")
GUARD_ENV = "ITERM_NOTE_HOOK"
MODEL = os.environ.get("NOTEBAR_MODEL", "haiku")
MAX_WORDS = 15
PREFIX_MAX_WORDS = 4
CLAUDE_TIMEOUT = 120
LOG = os.path.join(STATE_DIR, "notebar.log")


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), msg))
    except OSError:
        pass


def claude_bin():
    return (
        os.environ.get("CLAUDE_BIN")
        or shutil.which("claude")
        or os.path.expanduser("~/.local/bin/claude")
    )


# ---------------------------------------------------------------- state

def state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    return os.path.join(STATE_DIR, safe + ".json")


def load_state(session_id):
    try:
        with open(state_path(session_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(session_id, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    state["updated"] = int(time.time())
    tmp = state_path(session_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, state_path(session_id))


class Lock:
    """Per-session advisory lock so overlapping Stop hooks don't stack claude calls."""

    def __init__(self, session_id):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.path = state_path(session_id) + ".lock"
        self.fd = None

    def acquire(self):
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fd.close()
            return False


# ---------------------------------------------------------------- transcript

def tail_lines(path, max_bytes=262144):
    """Last lines of a file without reading the whole thing."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read()
    except OSError:
        return []
    lines = chunk.split(b"\n")
    if len(lines) > 1:
        lines = lines[1:]  # drop the (possibly partial) first line
    return [l for l in lines if l.strip()]


def _assistant_text(msg):
    content = msg.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(texts)
    return ""


def last_assistant_text(transcript_path, limit=1200):
    last = ""
    for raw in tail_lines(transcript_path):
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("type") != "assistant":
            continue
        text = _assistant_text(msg)
        if text.strip():
            last = text
    return last[:limit]


def first_user_text(transcript_path, limit=500):
    """First real user prompt, reading from the start and bailing early."""
    try:
        with open(transcript_path) as f:
            for raw in f:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("type") != "user":
                    continue
                content = msg.get("message", {}).get("content")
                if isinstance(content, list):
                    parts = [
                        b["text"]
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    content = "\n".join(parts)
                if not isinstance(content, str):
                    continue
                text = content.strip()
                # skip harness-injected messages (slash command envelopes etc.)
                if not text or text.startswith("<"):
                    continue
                return text[:limit]
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------- generation

def sanitize(text):
    line = ""
    for candidate in text.splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    line = line.strip("\"'` ")
    return re.sub(r"\s+", " ", line)


def clamp_words(text, n):
    words = text.split()
    return " ".join(words[:n])


def ask_claude(prompt, raw=False):
    env = dict(os.environ)
    env[GUARD_ENV] = "1"
    try:
        out = subprocess.run(
            [claude_bin(), "-p", prompt, "--model", MODEL],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            env=env,
            cwd=STATE_DIR,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log("claude call failed: %r" % e)
        return ""
    if out.returncode != 0:
        log("claude exit %d: %s" % (out.returncode, out.stderr.strip()[:200]))
        return ""
    result = out.stdout.strip() if raw else sanitize(out.stdout)
    if not result:
        log("claude returned empty output")
    return result


def gen_prefix(prompt_text):
    label = ask_claude(
        "A coding session just started with the user request below. Produce a "
        "2-4 word label naming the overall goal - it will be the permanent, "
        "never-changing prefix of a one-line status. Lowercase unless proper "
        "noun. No punctuation. Output ONLY the label.\n\nRequest: "
        + prompt_text[:600]
    )
    label = clamp_words(label, PREFIX_MAX_WORDS).rstrip(":")
    if not label:
        log("prefix generation fell back to prompt words")
        label = clamp_words(sanitize(prompt_text), PREFIX_MAX_WORDS)
    return label


def gen_update(goal, prev_status, user_excerpt, assistant_excerpt):
    """Returns (goal, status_line). The goal comes back unchanged unless the
    model judges the current label clearly wrong for what the session is
    actually building."""
    prompt = """You maintain a one-line status for a coding session, shown in a terminal status bar so the user can tell tabs apart and recall where each session left off. It tracks two facts:

1. GOAL - a 2-4 word label for the high-level thing being built. Sticky by design: repeat the current label EXACTLY unless it clearly misdescribes the session. (Sessions often open with an exploratory request before the real goal emerges; once the real goal is visible, correct the label - then keep the corrected label stable.)
2. THRUST - where work stands right now: <current state, 4-6 words> · next: <concrete next step, 3-5 words>. Omit "· next: ..." if there is no clear next step. Total display budget is {budget} words including the goal; every word must earn its place - concrete nouns and verbs, no filler.

Current GOAL label: {goal}
Previous status line: {prev}
Latest user request (excerpt): {user}
Agent's latest progress report (excerpt): {assistant}

Output exactly two lines, nothing else:
GOAL: <label>
THRUST: <thrust>""".format(
        goal=goal,
        budget=MAX_WORDS,
        prev=prev_status or "(none)",
        user=user_excerpt or "(none)",
        assistant=assistant_excerpt or "(none)",
    )
    reply = ask_claude(prompt, raw=True)
    if not reply:
        return goal, ""
    new_goal, thrust = goal, ""
    for line in reply.splitlines():
        line = sanitize(line)
        if line.upper().startswith("GOAL:"):
            candidate = clamp_words(line[5:].strip(), PREFIX_MAX_WORDS).rstrip(":")
            if candidate:
                new_goal = candidate
        elif line.upper().startswith("THRUST:"):
            thrust = line[7:].strip()
    if not thrust:
        return goal, ""
    if new_goal != goal:
        log("goal revised: %r -> %r" % (goal, new_goal))
    status = "%s: %s" % (new_goal, thrust)
    return new_goal, clamp_words(status, MAX_WORDS + 3)


# ---------------------------------------------------------------- display

def set_note(note):
    session_id = os.environ.get("ITERM_SESSION_ID", "").split(":")[-1]
    if not session_id:
        return
    try:
        import iterm2
    except ImportError:
        log("iterm2 module missing")
        return

    async def main(connection):
        app = await iterm2.async_get_app(connection)
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    if session.session_id == session_id:
                        await session.async_set_variable("user.note", note)
                        return

    try:
        iterm2.run_until_complete(main)
    except Exception as e:
        log("set_note failed: %r" % e)


# ---------------------------------------------------------------- handlers

def handle_prompt(event):
    session_id = event.get("session_id", "default")
    prompt = (event.get("prompt") or "").strip()
    if not prompt or prompt.startswith("/"):
        return
    state = load_state(session_id)
    if state.get("goal") or state.get("prefix"):
        state["last_prompt"] = prompt[:300]
        save_state(session_id, state)
        if state.get("status"):
            set_note(state["status"] + " ⋯")  # turn in progress
        return
    goal = gen_prefix(prompt)
    status = "%s: starting" % goal
    state.update({"goal": goal, "status": status, "last_prompt": prompt[:300]})
    save_state(session_id, state)
    set_note(status)


def handle_stop(event):
    session_id = event.get("session_id", "default")
    transcript = event.get("transcript_path", "")
    if not transcript or not os.path.isfile(transcript):
        return
    lock = Lock(session_id)
    if not lock.acquire():
        log("locked, skipping: %s" % session_id)
        return
    state = load_state(session_id)
    goal = state.get("goal") or state.get("prefix")
    if not goal:
        seed = first_user_text(transcript) or last_assistant_text(transcript, 400)
        if not seed:
            return
        goal = gen_prefix(seed)
    assistant = last_assistant_text(transcript)
    if not assistant:
        return
    goal, status = gen_update(
        goal, state.get("status", ""), state.get("last_prompt", ""), assistant
    )
    if not status:
        return
    state.pop("prefix", None)
    state["goal"] = goal
    state["status"] = status
    save_state(session_id, state)
    set_note(status)


def handle_session_start(event):
    session_id = event.get("session_id", "default")
    state = load_state(session_id)
    if state.get("status"):
        set_note(state["status"])
    prune_stale()


def prune_stale(days=14):
    cutoff = time.time() - days * 86400
    try:
        for name in os.listdir(STATE_DIR):
            path = os.path.join(STATE_DIR, name)
            if name.endswith((".json", ".lock")) and os.path.getmtime(path) < cutoff:
                os.unlink(path)
    except OSError:
        pass


def main():
    if os.environ.get(GUARD_ENV):
        return
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    handler = {
        "prompt": handle_prompt,
        "stop": handle_stop,
        "session-start": handle_session_start,
    }.get(command)
    if not handler:
        log("unknown command: %s" % command)
        return
    try:
        handler(event)
    except Exception as e:
        log("%s failed: %r" % (command, e))


if __name__ == "__main__":
    main()
