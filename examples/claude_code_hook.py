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
source, tool_name, is_interrupt, notification_type. It never reads the prompt,
tool inputs, tool results, the assistant's message, the transcript, or the
free text of a notification. The `detail` field is always a fixed template
("permission: Bash"). Lines are capped well under 4 KB and written with a
single O_APPEND write, so lines from parallel sessions never interleave.

It prints nothing (hook stdout can be injected into the model's context) and
always exits 0, so it can never block or steer a session.

Install (pinned copy, not the working tree)
-------------------------------------------
    mkdir -p ~/.claude/hooks
    cp examples/claude_code_hook.py ~/.claude/hooks/acc_claude_code_hook.py
    shasum -a 256 ~/.claude/hooks/acc_claude_code_hook.py   # record it

Then register it in ~/.claude/settings.json (see README, "Claude Code
sessions"). Use `"async": true` on PostToolUse and PostToolUseFailure, which
fire on every tool call.

Configuration (environment)
---------------------------
    ACC_EVENT_FILE   where to append events
                     (default ~/.agent-command-center/events.jsonl)
    ACC_STATE_DIR    per-session state for open approvals
                     (default <event file dir>/state)
    ACC_HOST         override the host label (default: short hostname)
    ACC_HOOK_DEBUG   set to 1 to log exceptions to <event file dir>/hook.log

Event mapping
-------------
    SessionStart            idle              "Session opened" / "Session resumed"
    UserPromptSubmit        started           "Working"
    PermissionRequest       awaiting_approval "Needs permission"  detail "permission: <tool>"
    Notification            awaiting_approval (permission_prompt only, as a fallback)
    PreToolUse              awaiting_approval "Question for you" (AskUserQuestion only)
    PostToolUse             approved          when it matches the open approval's tool
    PostToolUseFailure      approved, or declined "interrupted" when is_interrupt
    PermissionDenied        declined          when it matches
    Stop                    finished          "Your turn"
    SessionEnd              idle              "Session closed"

An approval still open at UserPromptSubmit, Stop or SessionEnd is closed as
declined with result "not run" (an interrupted prompt never fires Stop).
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
    return ev


# ── Approval lifecycle ──────────────────────────────────────────────────────────


def _new_approval_id(session_id: str) -> str:
    return "{}-{}".format(session_id[:8], uuid.uuid4().hex[:8])


def _action_for(kind: str) -> str:
    return "Question for you" if kind == "question" else "Needs permission"


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
            action=_action_for(current.get("kind", "permission")),
            status=status,
            approval_id=current.get("approval_id"),
            result=result,
        )
    )
    state.pop("open", None)
    save_state(session_id, state)
    return True


# ── Hook dispatch ───────────────────────────────────────────────────────────────


def handle(data: Dict[str, Any]) -> None:
    event = data.get("hook_event_name")
    if not isinstance(event, str):
        return
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = "unknown"
    agent = agent_name(data.get("cwd") if isinstance(data.get("cwd"), str) else "")
    tool_name = data.get("tool_name")
    tool_name = tool_name if isinstance(tool_name, str) else ""
    state = load_state(session_id)

    def emit(action: str, status: str, **kw: Any) -> None:
        append_event(make_event(agent=agent, session_id=session_id, action=action, status=status, **kw))

    if event == "SessionStart":
        source = data.get("source")
        if source in ("resume", "compact"):
            emit("Session resumed", "idle")
        else:
            clear_state(session_id)
            emit("Session opened", "idle")

    elif event == "UserPromptSubmit":
        close_approval(agent, session_id, state, status="declined", result="not run")
        emit("Working", "started")

    elif event == "PermissionRequest":
        kind = "question" if tool_name == QUESTION_TOOL else "permission"
        open_approval(agent, session_id, state, tool_name=tool_name, kind=kind)

    elif event == "PreToolUse":
        if tool_name == QUESTION_TOOL:
            open_approval(agent, session_id, state, tool_name=tool_name, kind="question")

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
        emit("Your turn", "finished")

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
