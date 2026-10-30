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
session's own title as the app shows it, when available. `where` is the
session's folder relative to your home folder ("Projects/Orion/app"), so the
dashboard can give projects friendly names; folders outside home are left out.

Opt-in, off by default: with ACC_DETECT_PAUSE=1 the prompt is checked in
memory for a request to pause ("please pause", "closing my laptop") and only a
`pause` flag is written; see is_pause_request().

One opt-in exception, off by default: with ACC_READ_TITLES=1 the script reads
the session's title from its history file (transcript_path), parsing only the
small `custom-title` lines and skipping every other line undecoded. See
title_from_transcript(). Lines are capped well under 4 KB and written with a
single O_APPEND write, so lines from parallel sessions never interleave.

Another opt-in exception, off by default: with ACC_SUMMARIES=1 the script also
keeps the session's own short words about its work: each task-list step's name
(update_plan items, TodoWrite todos), and from the desktop app's end-of-turn
wrap_up its recap, its question, whether it is blocked, and its buttons'
labels. Never a button's message, never any other tool's input. The text is
scrubbed (emails, URL passwords and queries, labelled values, long numbers,
token-shaped strings) and capped, then kept in one file per session beside
the state, replaced on every change, so only the latest turn exists. Files
older than 7 days are deleted. Events never carry this text. See save_summary().

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
    ACC_DETECT_PAUSE set to 1 to flag prompts that ask the session to pause
                     (matched in memory, never written; opt-in; off by default)
    ACC_READ_TITLES  set to 1 to take session titles from the history file's
                     `custom-title` lines (opt-in; off by default)
    ACC_SUMMARIES    set to 1 to keep step names and the end-of-turn recap,
                     question and button labels, scrubbed and capped, in
                     <state dir>/<session>.summary.json (opt-in; off by default)

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
WHERE_MAX = 160
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


def place(cwd: str) -> Optional[str]:
    """The session's folder relative to home ("Projects/Orion"), or None outside home."""
    cwd = (cwd or "").rstrip("/")
    marker = "/.claude/worktrees/"
    if marker in cwd:
        cwd = cwd.split(marker, 1)[0]
    home = os.path.expanduser("~").rstrip("/")
    if not home or not cwd.startswith(home + "/"):
        return None
    return cwd[len(home) + 1:][:WHERE_MAX] or None


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
    where: Optional[str] = None,
    level: str = "info",
    approval_id: Optional[str] = None,
    detail: Optional[str] = None,
    result: Optional[str] = None,
    title: Optional[str] = None,
    doing: Optional[str] = None,
    steps_done: Optional[int] = None,
    steps_total: Optional[int] = None,
    steps_current: Optional[int] = None,
    pause: Optional[bool] = None,
    time_limit: Optional[int] = None,
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
    if where:
        ev["where"] = where[:WHERE_MAX]
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
    if pause:
        ev["pause"] = True
    if time_limit:
        ev["time_limit"] = int(time_limit)
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
def _safe_history_path(path: Any) -> Optional[str]:
    """The real path of a session history file under ~/.claude/projects, else None."""
    if not isinstance(path, str) or not path:
        return None
    try:
        real = os.path.realpath(os.path.expanduser(path))
        allowed = os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    except OSError:
        return None
    if not real.startswith(allowed + os.sep) or not real.endswith(".jsonl"):
        return None
    return real


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
    if os.environ.get("ACC_READ_TITLES") != "1":
        return None
    real = _safe_history_path(path)
    if real is None:
        return None
    try:
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


def update_step_names(tool_name: str, tool_input: Any) -> Optional[list]:
    """
    Opt-in (ACC_SUMMARIES=1): the task list's step names after this tool call,
    or None when the call carries no fresh list (advance, done and start keep
    the names they had). Only `content` is read: never TodoWrite's activeForm.
    """
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "TodoWrite":
        items = tool_input.get("todos")
    elif _is_app_tool(tool_name, "update_plan"):
        items = tool_input.get("items")
    else:
        return None
    if not isinstance(items, list) or not items:
        return None
    return [scrub(it.get("content") if isinstance(it, dict) else None, STEP_NAME_MAX) or ""
            for it in items[:SUMMARY_STEPS_MAX]]


def current_step(steps: list) -> Optional[int]:
    """1-based number of the step in progress, else of the next pending one."""
    for i, s in enumerate(steps):
        if s == "in_progress":
            return i + 1
    for i, s in enumerate(steps):
        if s == "pending":
            return i + 1
    return None


# ── Turn summaries (opt-in) ─────────────────────────────────────────────────────
#
# With ACC_SUMMARIES=1, the session's own short words about its work: step
# names, and the desktop app's end-of-turn recap, question and button labels.
# Kept in <state dir>/<session>.summary.json, one file per session, replaced
# whole on every change, so only the latest turn exists; never in the events.

RECAP_MAX = 300
ASK_MAX = 100
STEP_NAME_MAX = 60
SUMMARY_STEPS_MAX = 12
OPTION_MAX = 24
OPTIONS_MAX = 2
SUMMARY_KEEP_DAYS = 7
_SUMMARY_SUFFIX = ".summary.json"
_SCRUB_INPUT_MAX = 4000


def summaries_on() -> bool:
    return os.environ.get("ACC_SUMMARIES") == "1"


# Common slips, not a guarantee: sessions must still never write secrets.
_URL_USER = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@")  # user:pass@ in any URL
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_LABELLED = re.compile(
    r"(?i)(?<![A-Za-z0-9])(password|passwd|passphrase|pwd|token|secret(?:[_ -]?access)?(?:[_ -]?key)?"
    r"|private[_ -]?key|api[_ -]?key)[\"']?\s*[:=]\s*\S+")
_PREFIXED = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-|[sr]k_(?:live|test)_|whsec_|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|glpat-"
    r"|xox[a-eprs]-|xapp-|AIza|eyJ)[A-Za-z0-9_\-.+/=]*"
    r"|(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"   # an AWS key id is exactly 20 characters
    r"|(?<![A-Za-z0-9])\d{8,10}:[A-Za-z0-9_-]{30,}")               # a Telegram bot token
_NUMBER = re.compile(r"\+?\d[\d ().\-]{8,}\d")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_IPV4 = re.compile(r"(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)")
_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
_HEX = re.compile(r"^[0-9a-fA-F]{32,}$")


def _char_class(c: str) -> int:
    return 0 if c.isdigit() else (1 if c.isupper() else 2)


def _token_like(run: str) -> bool:
    """A long run that reads as random (a key, a hash), not words or a path."""
    chars = [c for c in run if c.isalnum()]
    if not any(c.isdigit() for c in chars) or not any(c.isalpha() for c in chars):
        return False
    if _HEX.match("".join(chars)):
        return True
    switches = sum(1 for a, b in zip(chars, chars[1:]) if _char_class(a) != _char_class(b))
    return switches * 4 >= len(chars)


def _number(m: "re.Match") -> str:
    """Blank a run with 10 or more digits, not counting dates and IP addresses in it."""
    text = m.group(0)
    rest = _IPV4.sub("", _DATE.sub("", text))
    if sum(c.isdigit() for c in rest) < 10:
        return text
    return "[number]"


def scrub(value: Any, cap: int) -> Optional[str]:
    """One line of the session's own text, with likely-private bits blanked, capped."""
    if not isinstance(value, str):
        return None
    text = " ".join(value[:_SCRUB_INPUT_MAX].split())
    text = _URL_USER.sub(r"\1[secret]@", text)
    text = _URL_QUERY.sub(r"\1", text)
    text = _EMAIL.sub("[email]", text)
    text = _LABELLED.sub(lambda m: m.group(1) + ": [secret]", text)
    text = _PREFIXED.sub("[secret]", text)
    text = _NUMBER.sub(_number, text)
    text = _RUN.sub(lambda m: "[secret]" if _token_like(m.group(0)) else m.group(0), text)
    return text[:cap] or None


def _summary_path(session_id: str) -> str:
    return _state_path(session_id)[: -len(".json")] + _SUMMARY_SUFFIX


def load_summary(session_id: str) -> Dict[str, Any]:
    try:
        with open(_summary_path(session_id), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"steps": [], "steps_total": 0, "recap": None, "ask": None, "options": [], "blocked": False}


def save_summary(session_id: str, doc: Dict[str, Any]) -> None:
    """Replace the session's summary file whole (0600), so only the latest turn exists."""
    doc.update({"v": 1, "session": session_id[:8], "updated": _now_iso()})
    _ensure_dir(state_dir())
    path = _summary_path(session_id)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def prune_summaries() -> None:
    """Delete summary files untouched for SUMMARY_KEEP_DAYS (whether or not summaries are on)."""
    import time
    cutoff = time.time() - SUMMARY_KEEP_DAYS * 86400
    try:
        names = os.listdir(state_dir())
    except OSError:
        return
    for name in names:
        if not name.endswith(_SUMMARY_SUFFIX):
            continue
        path = os.path.join(state_dir(), name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def update_summary(doc: Dict[str, Any], steps: Optional[list], names: Optional[list],
                   tool_name: str, tool_input: Any) -> bool:
    """Fold one tool call into the summary. True when the file should be rewritten."""
    wrap = _is_app_tool(tool_name, "wrap_up")
    known = [x.get("name", "") for x in doc.get("steps") or [] if isinstance(x, dict)]
    if names is None and not (wrap or (steps is not None and known)):
        return False
    names = names if names is not None else known
    if steps is not None and names:
        doc["steps"] = [{"name": n, "status": s} for n, s in zip(names, steps[:SUMMARY_STEPS_MAX])]
        doc["steps_total"] = len(steps)
    if wrap:
        ti = tool_input if isinstance(tool_input, dict) else {}
        actions = ti.get("actions") if isinstance(ti.get("actions"), list) else []
        labels = [scrub(a.get("label"), OPTION_MAX) for a in actions if isinstance(a, dict)]
        doc.update({
            "recap": scrub(ti.get("recap"), RECAP_MAX),
            "ask": scrub(ti.get("ask"), ASK_MAX),
            "options": [x for x in labels if x][:OPTIONS_MAX],
            "blocked": ti.get("blocked") is True,
            "turn_at": _now_iso(),
        })
    return True


# ── Pause requests (opt-in) ─────────────────────────────────────────────────────
#
# "i need to close my laptop soon. please pause": with ACC_DETECT_PAUSE=1 the
# hook checks the prompt for a request to stop at a pause point. The text is
# matched in memory and never written; only a `pause` flag is. The patterns
# want a request to *the session* (please pause, pause here, find a stopping
# point, closing my laptop), so "add a pause button" or "when the host pauses
# the movie" do not count. Long prompts are skipped: a pause request is short.

_PAUSE_MAX_CHARS = 600
_PAUSE_CHECK_SECONDS = 20.0  # while working, look for a queued pause request at most this often
_PAUSE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bplease\s+pause\b",
    r"\bpause\s+(here|now|there|for\s+now|for\s+(the\s+)?(day|night|tonight|today)|this|work|the\s+work|on\s+this|at\s+a\s+good\s+(point|spot))\b",
    r"\b(find|get\s+to|reach|stop\s+at|look\s+for)\s+a\s+(good\s+|safe\s+|clean\s+|natural\s+)?(pause|pausing|stopping|break)\s+(point|spot)\b",
    r"\b(close|closing|shut|shutting)\s+(down\s+)?(my|the)\s+(laptop|computer|mac|macbook)\b",
    r"\b(stop|wrap\s+up|hold)\s+(here\s+)?for\s+(now|today|tonight|the\s+(day|night))\b",
    r"^\s*(ok(ay)?[,.]?\s+)?pause\s*(please|now|here|for\s+now)?\s*[.!]*\s*$",
    # "can you pause while i walk through the airport", "let's pause until tonight";
    # not "can you pause the video" (the app's own pause feature).
    r"\b(can|could|would|will)\s+you\s+(please\s+)?pause\b(?!\s+(the|a|an|my|our|this|that|it|their|everyone'?s?)\s+"
    r"(button|video|movie|film|show|stream|player|playback|music|song|audio|clip|trailer|party))",
    r"\blet'?s\s+pause\b(?!\s+(the|a|an|this|that|it)\s+(button|video|movie|film|show|stream|player|playback))",
    r"\bpause\s+(while|until|till)\b",
    r"\b(pick\s+(this|it)\s+up|pick\s+up|continue|resume|come\s+back\s+to\s+(this|it))\s+(later|tomorrow|tonight|in\s+a\s+bit)\b",
)]


_HUMAN_TAIL_BYTES = 1024 * 1024


def _human_text(rec: Dict[str, Any]) -> Optional[str]:
    """The text of a record the user typed, else None (tool results, notes, other sessions)."""
    if rec.get("isMeta") or rec.get("isSidechain"):
        return None
    if rec.get("type") == "attachment":
        a = rec.get("attachment")
        # A message sent while the session was working is queued, not a prompt.
        if not (isinstance(a, dict) and a.get("type") == "queued_command"):
            return None
        origin = a.get("origin")
        if not (isinstance(origin, dict) and origin.get("kind") == "human"):
            return None
        content = a.get("prompt")
    elif rec.get("type") == "user":
        content = (rec.get("message") or {}).get("content")
    else:
        return None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str) or not content.strip() or content.lstrip().startswith("<"):
        return None
    return content


def latest_human_message(path: Any) -> Optional[str]:
    """
    Opt-in (ACC_DETECT_PAUSE=1): the newest message the user typed in a session,
    including one sent while it was working (those arrive queued and fire no
    prompt hook). Read from the history file's tail, kept in memory, never written.
    """
    real = _safe_history_path(path)
    if real is None:
        return None
    try:
        size = os.path.getsize(real)
        with open(real, "rb") as f:
            for start in ((max(0, size - _HUMAN_TAIL_BYTES), 0) if size > _HUMAN_TAIL_BYTES else (0,)):
                f.seek(start)
                if start:
                    f.readline()  # skip a partial first line
                found = None
                for line in f:
                    if b'"user"' not in line and b'"queued_command"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    text = _human_text(rec) if isinstance(rec, dict) else None
                    if text is not None:
                        found = text
                if found is not None:
                    return found
    except OSError:
        return None
    return None


# "you have 10 minutes to complete this task, if not done then please pause":
# a time limit, not a pause request. Only the limit (in seconds) is kept.
_LIMIT_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\byou\s+have\s+(\d{1,4})\s*(minutes?|mins?|m|hours?|hrs?|h)\b",
    r"\b(pause|stop)\s+(in|after)\s+(\d{1,4})\s*(minutes?|mins?|m|hours?|hrs?|h)\b",
    r"\b(within|in\s+the\s+next)\s+(\d{1,4})\s*(minutes?|mins?|hours?|hrs?)\b",
    r"\b(do|finish|complete|get)\s+(it|this|that|everything)(\s+done)?\s+in\s+(\d{1,4})\s*(minutes?|mins?|m|hours?|hrs?|h)\b",
)]


def time_limit_seconds(prompt: Any) -> Optional[int]:
    """Seconds of a time limit the prompt sets (opt-in with ACC_DETECT_PAUSE=1), else None."""
    if os.environ.get("ACC_DETECT_PAUSE") != "1" or not isinstance(prompt, str):
        return None
    text = prompt.strip()
    if not text or len(text) > _PAUSE_MAX_CHARS:
        return None
    for pat in _LIMIT_PATTERNS:
        m = pat.search(text)
        if m:
            nums = [g for g in m.groups() if g and g.isdigit()]
            unit = m.groups()[-1].lower()
            if not nums:
                continue
            n = int(nums[0])
            secs = n * 3600 if unit.startswith("h") else n * 60
            if 0 < secs <= 24 * 3600:
                return secs
    return None


def is_pause_request(prompt: Any) -> bool:
    """True when ACC_DETECT_PAUSE=1 and the prompt asks the session to pause."""
    if os.environ.get("ACC_DETECT_PAUSE") != "1" or not isinstance(prompt, str):
        return False
    text = prompt.strip()
    if not text or len(text) > _PAUSE_MAX_CHARS:
        return False
    if time_limit_seconds(text):
        return False  # "pause if not done in 10 minutes" is a time limit
    return any(p.search(text) for p in _PAUSE_PATTERNS)


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
    agent: str, session_id: str, state: Dict[str, Any], *, tool_name: str, kind: str,
    where: Optional[str] = None,
) -> None:
    """Record a new open approval, closing any different one first."""
    current = state.get("open")
    if current:
        if current.get("tool_name") == tool_name and current.get("kind") == kind:
            return  # already shown; a second signal for the same prompt
        # A new prompt arrived while another was open: the old one is gone.
        close_approval(agent, session_id, state, status="declined", result="superseded", where=where)
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
            where=where,
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
    where: Optional[str] = None,
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
            where=where,
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
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    agent = agent_name(cwd)
    where = place(cwd)
    tool_name = data.get("tool_name")
    tool_name = tool_name if isinstance(tool_name, str) else ""
    state = load_state(session_id)

    def emit(action: str, status: str, **kw: Any) -> None:
        append_event(make_event(agent=agent, session_id=session_id, action=action, status=status, where=where, **kw))

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
                close_approval(agent, session_id, state, where=where, status="declined", result="not run")
            emit("Session resumed", "idle", title=title)
        else:
            clear_state(session_id)
            state = {}
            emit("Session opened", "idle", title=title)
        prune_summaries()

    elif event == "UserPromptSubmit":
        close_approval(agent, session_id, state, where=where, status="declined", result="not run")
        limit = time_limit_seconds(data.get("prompt"))
        pause = is_pause_request(data.get("prompt"))
        state.pop("pause", None)
        state.pop("limit", None)
        if pause:
            state["pause"] = True
        if limit:
            state["limit"] = limit
        emit("Working", "started", doing="Thinking", pause=pause or None, time_limit=limit)
        set_doing("Thinking")
        # A new prompt answers the last turn's question; its recap stays.
        if summaries_on() and os.path.exists(_summary_path(session_id)):
            doc = load_summary(session_id)
            if doc.get("ask") or doc.get("options") or doc.get("blocked"):
                doc.update({"ask": None, "options": [], "blocked": False})
                save_summary(session_id, doc)

    elif event == "PermissionRequest":
        kind = "question" if tool_name == QUESTION_TOOL else "permission"
        open_approval(agent, session_id, state, where=where, tool_name=tool_name, kind=kind)

    elif event == "PreToolUse":
        if tool_name == QUESTION_TOOL:
            open_approval(agent, session_id, state, where=where, tool_name=tool_name, kind="question")
        else:
            # Only when the activity changes, so a run of edits is one event.
            label = doing_label(tool_name)
            if label != state.get("doing"):
                emit("Working", "started", doing=label)
                set_doing(label)

    elif event == "Notification":
        if data.get("notification_type") == "permission_prompt" and not state.get("open"):
            open_approval(agent, session_id, state, where=where, tool_name="", kind="permission")

    elif event == "PostToolUse":
        current = state.get("open")
        if current and not current.get("tool_name"):
            # Opened from a Notification, which carries no tool name: any tool resolves it.
            close_approval(agent, session_id, state, where=where, status="approved")
        else:
            result = "answered" if tool_name == QUESTION_TOOL else None
            close_approval(agent, session_id, state, where=where, status="approved", result=result, tool_name=tool_name)
        # A pause request sent mid-turn arrives queued: spot it early, so the
        # session shows "Wrapping up to pause" before it stops.
        if os.environ.get("ACC_DETECT_PAUSE") == "1" and not state.get("pause"):
            import time
            now = time.time()
            if now - float(state.get("pause_checked") or 0) >= _PAUSE_CHECK_SECONDS:
                state["pause_checked"] = now
                latest = latest_human_message(data.get("transcript_path"))
                limit = time_limit_seconds(latest)
                if limit and not state.get("limit"):
                    state["limit"] = limit
                    emit("Time limit set", "info", time_limit=limit)
                elif is_pause_request(latest):
                    state["pause"] = True
                    emit("Pause requested", "info", pause=True)
                save_state(session_id, state)
        # A task-list update: report how far along the session is (counts only).
        steps = update_steps(state.get("steps"), tool_name, data.get("tool_input"))
        # Only the task-list tools and wrap_up touch the summary; skip the read otherwise.
        if summaries_on() and (steps is not None or _is_app_tool(tool_name, "wrap_up")):
            doc = load_summary(session_id)
            names = update_step_names(tool_name, data.get("tool_input"))
            if update_summary(doc, steps, names, tool_name, data.get("tool_input")):
                save_summary(session_id, doc)
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
            close_approval(agent, session_id, state, where=where, status="declined", result="interrupted", tool_name=match_tool)
        else:
            close_approval(agent, session_id, state, where=where, status="approved", result="tool failed", tool_name=match_tool)

    elif event == "PermissionDenied":
        current = state.get("open")
        match_tool = None if (current and not current.get("tool_name")) else tool_name
        close_approval(agent, session_id, state, where=where, status="declined", tool_name=match_tool)

    elif event == "Stop":
        close_approval(agent, session_id, state, where=where, status="declined", result="not run")
        # The app names a session after its first turn and on every rename.
        # Paused when the turn began with a pause request, or when the newest
        # message the user typed (possibly sent mid-turn, which fires no prompt
        # hook) asks to pause.
        pause = bool(state.get("pause")) or (
            os.environ.get("ACC_DETECT_PAUSE") == "1"
            and is_pause_request(latest_human_message(data.get("transcript_path")))
        )
        emit("Your turn", "finished", title=title_from_transcript(data.get("transcript_path")),
             pause=pause or None)
        set_doing(None)

    elif event == "SessionEnd":
        close_approval(agent, session_id, state, where=where, status="declined", result="not run")
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
