#!/usr/bin/env python3
"""
Claude Code -> Agent Command Center emitter.

A Claude Code *hook*: Claude Code runs this script at points in a session
(session start, a prompt sent, a permission request, a tool finishing, the
turn ending) and pipes a JSON description of the moment to stdin. This script
turns each moment into one acc/1 event appended to the dashboard's event file,
so every Claude Code session shows up as an agent card, and every permission
prompt or question waiting on you shows up in the approvals panel.

Privacy, by construction
------------------------
The script reads only these stdin fields: hook_event_name, session_id, cwd,
source, session_title, tool_name, is_interrupt, notification_type. It never
reads the prompt, tool results, the assistant's message, the transcript, or
the free text of a notification, and reads tool inputs in one case only: for
a task-list update (TodoWrite, the app's update_plan) it reads each step's
status value and the list's length, never a step's text, and reports counts
("step 3 of 6"). See update_steps(). The `detail` field is always a
fixed template ("permission: Bash"), and `doing` is a fixed phrase chosen from
the tool name alone ("Editing files", "Running a command"). `title` is the
session's own title as the app shows it, when available.

One opt-in exception, off by default: with ACC_READ_TITLES=1 the script reads
the session's title from its history file (transcript_path), parsing only the
small `custom-title` lines and skipping every other line undecoded. See
title_from_transcript(). Lines are capped well under 4 KB and written with a
single O_APPEND write, so lines from parallel sessions never interleave.

It prints nothing (hook stdout can be injected into the model's context) and
always exits 0, so it can never block or steer a session.

Install (pinned copy, not the working tree)
-------------------------------------------
    mkdir -p ~/.claude/hooks
    cp examples/claude_code_hook.py ~/.claude/hooks/acc_claude_code_hook.py
    shasum -a 256 ~/.claude/hooks/acc_claude_code_hook.py   # record it

Then register it in ~/.claude/settings.json (see README, "Claude Code
sessions"). Use `"async": true` on PreToolUse, PostToolUse and
PostToolUseFailure, which fire on every tool call.

Configuration (environment)
---------------------------
    ACC_EVENT_FILE   where to append events
                     (default ~/.agent-command-center/events.jsonl)
    ACC_STATE_DIR    per-session state for open approvals
                     (default <event file dir>/state)
    ACC_HOST         override the host label (default: short hostname)
    ACC_HOOK_DEBUG   set to 1 to log exceptions to <event file dir>/hook.log
    ACC_READ_TITLES  set to 1 to take session titles from the history file's
                     `custom-title` lines (opt-in; off by default)

Event mapping
-------------
    SessionStart            idle              "Session opened" / "Session resumed" (+ title)
    UserPromptSubmit        started           "Working", doing "Thinking"
    PermissionRequest       awaiting_approval "Needs permission"  detail "permission: <tool>"
    Notification            awaiting_approval (permission_prompt only, as a fallback)
    PreToolUse              awaiting_approval "Question for you" (AskUserQuestion), else
                            started "Working" with `doing`, only when the activity changes
    PostToolUse             approved          when it matches the open approval's tool
    PostToolUseFailure      approved, or declined "interrupted" when is_interrupt
    PermissionDenied        declined          when it matches
    Stop                    finished          "Your turn"
    SessionEnd              idle              "Session closed"

Resolve events carry their own action ("Permission granted", "Permission
denied", "Question answered", "Prompt dismissed", "Interrupted"). An approval
still open at UserPromptSubmit, Stop or SessionEnd is closed as declined with
result "not run" (an interrupted prompt never fires Stop).
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SCHEMA = "acc/1"
MAX_LINE_BYTES = 3500
DETAIL_MAX = 64
TITLE_MAX = 100
QUESTION_TOOL = "AskUserQuestion"

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


# ── Paths ───────────────────────────────────────────────────────────────────────


def event_file() -> str:
    path = os.environ.get("ACC_EVENT_FILE") or os.path.join(
        os.path.expanduser("~"), ".agent-command-center", "events.jsonl"
    )
    return os.path.expanduser(path)


def state_dir() -> str:
    path = os.environ.get("ACC_STATE_DIR") or os.path.join(
        os.path.dirname(event_file()), "state"
    )
    return os.path.expanduser(path)


def _ensure_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)


def _state_path(session_id: str) -> str:
    safe = _SAFE_ID.sub("_", session_id)[:80] or "unknown"
    return os.path.join(state_dir(), safe + ".json")


# ── Per-session state (the one open approval, if any) ───────────────────────────


def load_state(session_id: str) -> Dict[str, Any]:
    try:
        with open(_state_path(session_id), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(session_id: str, state: Dict[str, Any]) -> None:
    _ensure_dir(state_dir())
    path = _state_path(session_id)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def clear_state(session_id: str) -> None:
    try:
        os.remove(_state_path(session_id))
    except OSError:
        pass
    # The session's .lock file stays (removing a held lock file would let a
    # second hook lock a fresh one); it is a few bytes per session.


# ── Event writing ───────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _host() -> str:
    override = os.environ.get("ACC_HOST")
    if override:
        return override[:40]
    try:
        return socket.gethostname().split(".")[0][:40]
    except OSError:
        return "unknown"


def agent_name(cwd: str) -> str:
    """The project label for a session: its folder name, with two tidy-ups."""
    cwd = (cwd or "").rstrip("/")
    # A Claude Code worktree lives at <repo>/.claude/worktrees/<name>; name the repo.
    marker = "/.claude/worktrees/"
    if marker in cwd:
        cwd = cwd.split(marker, 1)[0]
    name = os.path.basename(cwd) or "unknown"
    # Scratch workspaces are throwaway folders with dated names.
    if name.startswith("scratch-"):
        return "scratch"
    return name[:60]


def append_event(payload: Dict[str, Any]) -> None:
    path = event_file()
    _ensure_dir(os.path.dirname(path))
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")
    if len(data) > MAX_LINE_BYTES:
        # Should never happen with fixed templates; drop rather than risk a torn line.
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def make_event(
    *,
    agent: str,
    session_id: str,
    action: str,
    status: str,
    level: str = "info",
    approval_id: Optional[str] = None,
    detail: Optional[str] = None,
    result: Optional[str] = None,
    title: Optional[str] = None,
    doing: Optional[str] = None,
    steps_done: Optional[int] = None,
    steps_total: Optional[int] = None,
    steps_current: Optional[int] = None,
) -> Dict[str, Any]:
    ev: Dict[str, Any] = {
        "schema": SCHEMA,
        "id": str(uuid.uuid4()),
        "ts": _now_iso(),
        "agent": agent,
        "action": action,
        "status": status,
        "level": level,
        "source": "claude-code",
        "host": _host(),
        "session": session_id[:8],
    }
    if approval_id:
        ev["approval_id"] = approval_id
    if detail:
        ev["detail"] = detail[:DETAIL_MAX]
    if result:
        ev["result"] = result[:DETAIL_MAX]
    if title:
        ev["title"] = title[:TITLE_MAX]
    if doing:
        ev["doing"] = doing[:DETAIL_MAX]
    if steps_total:
        ev["steps_done"] = int(steps_done or 0)
        ev["steps_total"] = int(steps_total)
        if steps_current:
            ev["steps_current"] = int(steps_current)
    return ev


# ── Plain-language labels ───────────────────────────────────────────────────────

_DOING = {
    "Edit": "Editing files",
    "MultiEdit": "Editing files",
    "Write": "Editing files",
    "NotebookEdit": "Editing files",
    "Read": "Reading files",
    "Grep": "Searching the code",
    "Glob": "Searching the code",
    "LS": "Searching the code",
    "Bash": "Running a command",
    "BashOutput": "Checking a command",
    "KillShell": "Checking a command",
    "WebFetch": "Browsing the web",
    "WebSearch": "Browsing the web",
    "Task": "Running a sub-agent",
    "Agent": "Running a sub-agent",
    "TodoWrite": "Planning",
    "ExitPlanMode": "Planning",
    "EnterPlanMode": "Planning",
    "Skill": "Using a skill",
    "ToolSearch": "Loading tools",
    "Artifact": "Publishing a page",
    "SendUserFile": "Sending you a file",
    "SendMessage": "Messaging another session",
}
# The Claude desktop app's own tools ("mcp__ccd_<area>__<tool>").
_APP_DOING = {
    "ccd_turn": "Updating its task list",
    "ccd_session": "Managing sessions",
    "ccd_session_mgmt": "Managing sessions",
    "ccd_pr": "Checking the pull request",
    "ccd_sidebar": "Organizing the sidebar",
}
_HEXISH = re.compile(r"^[0-9a-f-]{16,}$")


def doing_label(tool_name: str) -> str:
    """What a session is doing, in plain words, from the tool name alone."""
    if tool_name in _DOING:
        return _DOING[tool_name]
    if tool_name.startswith("mcp__"):
        server = tool_name.split("__")[1] if tool_name.count("__") >= 2 else ""
        low = server.lower()
        if "browser" in low or "chrome" in low:
            return "Using the browser"
        if low.startswith("ccd_"):
            return _APP_DOING.get(low, "Using the app")
        if not server or _HEXISH.match(low):
            return "Using a connector"
        return "Using {}".format(server.replace("_", " ").strip()[:30])
    return "Using {}".format(tool_name[:30]) if tool_name else "Working"


def clean_title(value: Any) -> Optional[str]:
    """A session title as shown in the app, on one line; None when absent."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:TITLE_MAX] or None


# A record's own "type" key, compact or spaced. Text quoted inside a message is
# JSON-escaped (\"type\":\"...\"), so it never matches these byte patterns.
_TITLE_MARKS = (b'"type":"custom-title"', b'"type": "custom-title"')
_TITLE_TAIL_BYTES = 512 * 1024


def title_from_transcript(path: Any) -> Optional[str]:
    """
    Opt-in (ACC_READ_TITLES=1): the session's title as the app shows it.

    Claude Code does not pass the title to hooks, but the session's own
    history file records each title as a small `custom-title` line. Only those
    lines are parsed; every other line is skipped unread, so no prompt, reply
    or tool content is ever decoded. Reads the last 512 KB first (titles are
    re-recorded as the session goes) and the whole file only if needed. Only
    files under ~/.claude/projects are opened.
    """
    if os.environ.get("ACC_READ_TITLES") != "1" or not isinstance(path, str) or not path:
        return None
    try:
        real = os.path.realpath(os.path.expanduser(path))
        allowed = os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude", "projects"))
        if not real.startswith(allowed + os.sep) or not real.endswith(".jsonl"):
            return None
        size = os.path.getsize(real)
        with open(real, "rb") as f:
            for start in ((max(0, size - _TITLE_TAIL_BYTES), 0) if size > _TITLE_TAIL_BYTES else (0,)):
                f.seek(start)
                found = None
                for line in f:
                    if not any(mark in line for mark in _TITLE_MARKS):
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("type") == "custom-title":
                        found = clean_title(rec.get("customTitle")) or found
                if found:
                    return found
    except OSError:
        return None
    return None


# ── Step progress (task lists) ──────────────────────────────────────────────────
#
# Sessions keep a task list: TodoWrite in the terminal, the desktop app's
# update_plan, closed out by its wrap_up. From those inputs only the steps'
# *status* values are read ("pending", "in_progress", "completed"), never any
# step's text. The state keeps that list of statuses; events carry counts.

_STEP_STATUSES = ("pending", "in_progress", "completed")
_MAX_STEPS = 50


def _statuses(items: Any) -> Optional[list]:
    if not isinstance(items, list) or not items:
        return None
    out = []
    for it in items[:_MAX_STEPS]:
        status = it.get("status") if isinstance(it, dict) else None
        out.append(status if status in _STEP_STATUSES else "pending")
    return out


def _is_app_tool(tool_name: str, name: str) -> bool:
    return tool_name.startswith("mcp__") and tool_name.endswith("__" + name) and "ccd_turn" in tool_name


def update_steps(steps: Any, tool_name: str, tool_input: Any) -> Optional[list]:
    """The task list's statuses after this tool call, or None if it is not a task-list update."""
    steps = list(steps) if isinstance(steps, list) else None
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool_name == "TodoWrite":
        return _statuses(tool_input.get("todos"))
    if _is_app_tool(tool_name, "wrap_up"):
        return ["completed"] * len(steps) if steps else None
    if not _is_app_tool(tool_name, "update_plan"):
        return None
    fresh = _statuses(tool_input.get("items"))
    if fresh is not None:
        return fresh
    if not steps:
        return None
    if tool_input.get("advance") is True:
        cur = next((i for i, s in enumerate(steps) if s == "in_progress"), None)
        if cur is not None:
            steps[cur] = "completed"
        nxt = next((i for i, s in enumerate(steps) if s == "pending"), None)
        if nxt is not None:
            steps[nxt] = "in_progress"
        return steps
    done, start = tool_input.get("done"), tool_input.get("start")
    if isinstance(done, list):
        for n in done:
            if isinstance(n, int) and 1 <= n <= len(steps):
                steps[n - 1] = "completed"
    if isinstance(start, int) and 1 <= start <= len(steps):
        steps = ["pending" if s == "in_progress" else s for s in steps]
        steps[start - 1] = "in_progress"
    return steps


def current_step(steps: list) -> Optional[int]:
    """1-based number of the step in progress, else of the next pending one."""
    for i, s in enumerate(steps):
        if s == "in_progress":
            return i + 1
    for i, s in enumerate(steps):
        if s == "pending":
            return i + 1
    return None


# ── Approval lifecycle ──────────────────────────────────────────────────────────


def _new_approval_id(session_id: str) -> str:
    return "{}-{}".format(session_id[:8], uuid.uuid4().hex[:8])


def _action_for(kind: str) -> str:
    return "Question for you" if kind == "question" else "Needs permission"


def _resolution_action(kind: str, status: str, result: Optional[str]) -> str:
    """The resolve event's label, so a card never repeats a stale ask."""
    question = kind == "question"
    if status == "approved":
        return "Question answered" if question else "Permission granted"
    if result == "interrupted":
        return "Interrupted"
    if result in ("not run", "superseded"):
        return "Question skipped" if question else "Prompt dismissed"
    return "Question skipped" if question else "Permission denied"


def open_approval(
    agent: str, session_id: str, state: Dict[str, Any], *, tool_name: str, kind: str
) -> None:
    """Record a new open approval, closing any different one first."""
    current = state.get("open")
    if current:
        if current.get("tool_name") == tool_name and current.get("kind") == kind:
            return  # already shown; a second signal for the same prompt
        # A new prompt arrived while another was open: the old one is gone.
        close_approval(agent, session_id, state, status="declined", result="superseded")
    approval_id = _new_approval_id(session_id)
    if kind == "question":
        detail = "question"
    elif tool_name:
        detail = "permission: {}".format(tool_name)
    else:
        detail = "permission"
    append_event(
        make_event(
            agent=agent,
            session_id=session_id,
            action=_action_for(kind),
            status="awaiting_approval",
            level="flash",
            approval_id=approval_id,
            detail=detail,
        )
    )
    state["open"] = {"approval_id": approval_id, "tool_name": tool_name, "kind": kind}
    save_state(session_id, state)


def close_approval(
    agent: str,
    session_id: str,
    state: Dict[str, Any],
    *,
    status: str,
    result: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> bool:
    """
    Resolve the open approval. With tool_name given, only when it matches.
    Returns True when an event was written.
    """
    current = state.get("open")
    if not current:
        return False
    if tool_name is not None and current.get("tool_name") != tool_name:
        return False
    append_event(
        make_event(
            agent=agent,
            session_id=session_id,
            action=_resolution_action(current.get("kind", "permission"), status, result),
            status=status,
            approval_id=current.get("approval_id"),
            result=result,
        )
    )
    state.pop("open", None)
    save_state(session_id, state)
    return True


# ── Hook dispatch ───────────────────────────────────────────────────────────────


_LOCK_WAIT_SECONDS = 3.0


class _SessionLock:
    """
    One hook at a time per session. Some hooks run in the background and can
    fire at the same instant (PermissionRequest and PreToolUse for one
    question); without this both read "nothing open" and both open an
    approval, and only one ever gets closed. Waits up to 3 s, then proceeds
    unlocked rather than ever holding up the session.
    """

    def __init__(self, session_id: str):
        self.path = _state_path(session_id)[: -len(".json")] + ".lock"
        self.fd: Optional[int] = None

    def __enter__(self):
        try:
            import fcntl
            import time
            _ensure_dir(state_dir())
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            deadline = time.monotonic() + _LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        break
                    time.sleep(0.01)
        except (OSError, ImportError):
            pass
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)  # closing releases the lock
            except OSError:
                pass
        return False


def handle(data: Dict[str, Any]) -> None:
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = "unknown"
    with _SessionLock(session_id):
        _dispatch(data, session_id)


def _dispatch(data: Dict[str, Any], session_id: str) -> None:
    event = data.get("hook_event_name")
    if not isinstance(event, str):
        return
    agent = agent_name(data.get("cwd") if isinstance(data.get("cwd"), str) else "")
    tool_name = data.get("tool_name")
    tool_name = tool_name if isinstance(tool_name, str) else ""
    state = load_state(session_id)

    def emit(action: str, status: str, **kw: Any) -> None:
        append_event(make_event(agent=agent, session_id=session_id, action=action, status=status, **kw))

    def set_doing(label: Optional[str]) -> None:
        if label:
            state["doing"] = label
        else:
            state.pop("doing", None)
        save_state(session_id, state)

    if event == "SessionStart":
        source = data.get("source")
        title = clean_title(data.get("session_title")) or title_from_transcript(data.get("transcript_path"))
        if source in ("resume", "compact"):
            if source == "resume":
                # A prompt that was open when the app quit or the machine
                # restarted is gone; resuming does not bring it back.
                close_approval(agent, session_id, state, status="declined", result="not run")
            emit("Session resumed", "idle", title=title)
        else:
            clear_state(session_id)
            state = {}
            emit("Session opened", "idle", title=title)

    elif event == "UserPromptSubmit":
        close_approval(agent, session_id, state, status="declined", result="not run")
        emit("Working", "started", doing="Thinking")
        set_doing("Thinking")

    elif event == "PermissionRequest":
        kind = "question" if tool_name == QUESTION_TOOL else "permission"
        open_approval(agent, session_id, state, tool_name=tool_name, kind=kind)

    elif event == "PreToolUse":
        if tool_name == QUESTION_TOOL:
            open_approval(agent, session_id, state, tool_name=tool_name, kind="question")
        else:
            # Only when the activity changes, so a run of edits is one event.
            label = doing_label(tool_name)
            if label != state.get("doing"):
                emit("Working", "started", doing=label)
                set_doing(label)

    elif event == "Notification":
        if data.get("notification_type") == "permission_prompt" and not state.get("open"):
            open_approval(agent, session_id, state, tool_name="", kind="permission")

    elif event == "PostToolUse":
        current = state.get("open")
        if current and not current.get("tool_name"):
            # Opened from a Notification, which carries no tool name: any tool resolves it.
            close_approval(agent, session_id, state, status="approved")
        else:
            result = "answered" if tool_name == QUESTION_TOOL else None
            close_approval(agent, session_id, state, status="approved", result=result, tool_name=tool_name)
        # A task-list update: report how far along the session is (counts only).
        steps = update_steps(state.get("steps"), tool_name, data.get("tool_input"))
        if steps is not None and steps != state.get("steps"):
            state["steps"] = steps
            save_state(session_id, state)
            done = sum(1 for s in steps if s == "completed")
            emit("Working", "started", doing=state.get("doing"), steps_done=done, steps_total=len(steps),
                 steps_current=current_step(steps))

    elif event == "PostToolUseFailure":
        current = state.get("open")
        match_tool = None if (current and not current.get("tool_name")) else tool_name
        if data.get("is_interrupt") is True:
            close_approval(agent, session_id, state, status="declined", result="interrupted", tool_name=match_tool)
        else:
            close_approval(agent, session_id, state, status="approved", result="tool failed", tool_name=match_tool)

    elif event == "PermissionDenied":
        current = state.get("open")
        match_tool = None if (current and not current.get("tool_name")) else tool_name
        close_approval(agent, session_id, state, status="declined", tool_name=match_tool)

    elif event == "Stop":
        close_approval(agent, session_id, state, status="declined", result="not run")
        # The app names a session after its first turn and on every rename.
        emit("Your turn", "finished", title=title_from_transcript(data.get("transcript_path")))
        set_doing(None)

    elif event == "SessionEnd":
        close_approval(agent, session_id, state, status="declined", result="not run")
        emit("Session closed", "idle")
        clear_state(session_id)

    # Every other hook event is ignored on purpose.


def _debug_log(exc: BaseException) -> None:
    if os.environ.get("ACC_HOOK_DEBUG") != "1":
        return
    try:
        path = os.path.join(os.path.dirname(event_file()), "hook.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("{} {}: {}\n".format(_now_iso(), type(exc).__name__, exc))
    except OSError:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            handle(data)
    except BaseException as exc:  # never let a hook failure reach the session
        _debug_log(exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
