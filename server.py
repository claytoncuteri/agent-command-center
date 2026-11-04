#!/usr/bin/env python3
"""
Agent Command Center — local web server.

Reads an append-only events.jsonl file and serves a live dashboard.
Bind address is 127.0.0.1 by default. For remote access use a private
mesh (e.g. Tailscale) — never expose this port directly to the internet.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import pathlib
import re
import secrets
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── Config ─────────────────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    # One file, or several merged into one view (e.g. a laptop stream plus a
    # server stream). "event_files" wins when set; "event_file" is kept for
    # older configs. Missing files are tolerated and simply contribute nothing.
    "event_file": "./events.jsonl",
    "event_files": None,
    "bind": "127.0.0.1",
    "port": 7000,
    "title": "Agent Command Center",
    "logo": None,
    "billing_mode": "max",
    "usage": {
        "window_label": "SESSION",
        "reset_label": "clears on page reload",
        "tokens_max": None,
    },
    # Action mode: "readonly" (default, safe), "browser", "telegram", or "both".
    "actions_mode": "readonly",
    # Shared secret for the browser write-back endpoint. Null → auto-generated.
    "actions_token": None,
    # Content mode: "inline" (draft in the stream) or "reference" (pointer on disk).
    "content_mode": "inline",
    # Sandbox root for reference-mode drafts. Null → directory of event_file.
    "content_dir": None,
    # Where Revise+Submit decisions are logged (separate from the event stream).
    "corrections_file": "./corrections.jsonl",
    "telegram": None,
    # Which page "/" serves: "sessions" (one row per session, needs-you first),
    # "focus" (one session in depth, following the one you have in front; built
    # for a tall second screen) or "classic" (agent cards and the approvals
    # workflow). All stay reachable at /sessions, /focus and /classic.
    "view": "sessions",
    # Friendly project names: {"Projects/Orion": "Orion", "orion-*": "Orion"}.
    # A key with a slash is a folder under home (and everything inside it);
    # otherwise it is a folder name, with * standing for the rest.
    "project_names": {},
    # macOS + Claude desktop app only: link each Claude Code session row to the
    # same session in the app ("Open in Claude"), and let the focus view follow
    # the session the app has in front. Reads five fields from the app's
    # per-session records: two ids, when the session was last focused, its model
    # and its effort. Off unless you turn it on.
    "claude_app_links": False,
    # Serve the hook's opt-in turn summaries (ACC_SUMMARIES=1): each session's
    # step names and its end-of-turn recap, question and button labels. Off
    # unless you turn it on. summaries_dir null = the "state" folder beside the
    # first event file, where the hook keeps them.
    "turn_summaries": False,
    "summaries_dir": None,
    # Remote viewing through Tailscale serve (or any local reverse proxy).
    # A request that arrives with proxy headers is "remote" and is allowed only
    # when its X-Forwarded-For is one of these exact addresses. Empty = no
    # remote viewers (fail closed). Direct local requests are unaffected.
    "remote_clients": [],
    # Optional PNG served at /apple-touch-icon.png (the iPhone home-screen icon).
    "touch_icon": None,
}

_VIEWS = {"sessions": "sessions.html", "focus": "focus.html", "classic": "index.html"}

_ACTION_MODES = {"readonly", "browser", "telegram", "both"}
# Modes in which the in-browser write-back endpoint is live.
_BROWSER_ACTION_MODES = {"browser", "both"}
_CONTENT_MODES = {"inline", "reference"}


def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    cfg_path = pathlib.Path("config.json")
    if cfg_path.exists():
        try:
            with cfg_path.open(encoding="utf-8") as f:
                overrides = json.load(f)
            # Drop comment/documentation keys (any key starting with "_").
            overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}
            cfg.update(overrides)
        except Exception as exc:
            print(f"[acc] Warning: could not load config.json — {exc}")
    # Normalise the event source(s) into a list of expanded paths.
    files = cfg.get("event_files")
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, list) or not files:
        files = [cfg.get("event_file") or _DEFAULTS["event_file"]]
    cfg["event_files"] = [
        str(pathlib.Path(p).expanduser()) for p in files if isinstance(p, str) and p.strip()
    ] or [_DEFAULTS["event_file"]]
    # The first file stays the primary one (content sandbox root, legacy readers).
    cfg["event_file"] = cfg["event_files"][0]
    # Validate action mode, fall back to the safe default.
    mode = cfg.get("actions_mode")
    if mode not in _ACTION_MODES:
        if mode is not None:
            print(f"[acc] Warning: unknown actions_mode {mode!r} — falling back to 'readonly'.")
        cfg["actions_mode"] = "readonly"
    # Validate the default view.
    view = cfg.get("view")
    if view not in _VIEWS:
        if view is not None:
            print(f"[acc] Warning: unknown view {view!r}, falling back to 'sessions'.")
        cfg["view"] = "sessions"
    # Validate content mode, fall back to the simple default.
    cmode = cfg.get("content_mode")
    if cmode not in _CONTENT_MODES:
        if cmode is not None:
            print(f"[acc] Warning: unknown content_mode {cmode!r} — falling back to 'inline'.")
        cfg["content_mode"] = "inline"
    return cfg


CONFIG = load_config()


def _telegram_configured() -> bool:
    tg = CONFIG.get("telegram")
    return bool(isinstance(tg, dict) and tg.get("bot_token") and tg.get("chat_id"))


# Token the browser must present to the write-back endpoint (browser mode only).
# A stable value from config.json survives restarts; otherwise one is generated
# per server start. The token is only handed to the same-origin dashboard page.
ACTIONS_TOKEN: str = CONFIG.get("actions_token") or secrets.token_urlsafe(24)

# ── Event parsing ──────────────────────────────────────────────────────────────


def parse_events(path: str) -> list[dict]:
    """Read all valid JSON objects from an NDJSON file. Skip bad lines silently."""
    events: list[dict] = []
    p = pathlib.Path(path)
    if not p.exists():
        return events
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        events.append(obj)
                except json.JSONDecodeError:
                    pass  # malformed line — skip, never crash
    except OSError:
        pass
    return events


def parse_all_events(paths: list[str]) -> list[dict]:
    """Merge the events of several NDJSON files. The UI orders them by `ts`."""
    events: list[dict] = []
    for path in paths:
        events.extend(parse_events(path))
    return events


def _sizes(paths: list[str]) -> tuple:
    """Current size of each source file (0 when missing); changes trigger a push."""
    out = []
    for path in paths:
        try:
            out.append(pathlib.Path(path).stat().st_size)
        except OSError:
            out.append(0)
    return tuple(out)


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="Agent Command Center", docs_url=None, redoc_url=None)


# ── Remote gate ─────────────────────────────────────────────────────────────────
#
# The server binds to loopback. Remote viewers reach it only through a local
# reverse proxy such as `tailscale serve`, which replaces any client-sent
# X-Forwarded-For and Tailscale-User-* headers with the real tailnet address
# and identity (checked against Tailscale 1.104.1 on macOS). So: a loopback request with
# no proxy headers is the machine's own browser and is allowed; a request with
# proxy headers is allowed only when X-Forwarded-For is a single address on the
# remote_clients list. Anything else gets 403.

_PROXY_HEADERS = (
    b"x-forwarded-for", b"x-forwarded-host", b"forwarded",
    b"tailscale-user-login", b"tailscale-headers-info",
)
_LOOPBACK = {"127.0.0.1", "::1"}
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}


def _host_name(headers: list) -> str:
    """The Host header's name part, without the port ("[::1]" kept bracketed)."""
    host = next((v.decode("latin-1").strip().lower() for k, v in headers if k.lower() == b"host"), "")
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def gate_decision(client_host: Optional[str], headers: list, allowed: list) -> tuple:
    """(allowed, reason) for one request; headers are ASGI (bytes, bytes) pairs."""
    if client_host not in _LOOPBACK:
        return False, "not loopback"
    names = {k.lower() for k, _ in headers}
    if not any(h in names for h in _PROXY_HEADERS):
        # A local request must also name a local host. This refuses DNS-rebinding
        # pages in the local browser and forwarders that add no proxy headers.
        if _host_name(headers) not in _LOCAL_HOSTNAMES:
            return False, "local request for a non-local host"
        return True, "local"
    xff = [v.decode("latin-1").strip() for k, v in headers if k.lower() == b"x-forwarded-for"]
    if len(xff) != 1 or not xff[0] or "," in xff[0]:
        return False, "no single forwarded address"
    if xff[0] in {str(a) for a in (allowed or [])}:
        return True, "remote allowed"
    return False, "remote not allowed"


class RemoteGate:
    """ASGI middleware applying gate_decision to every HTTP request, streams included."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.inner(scope, receive, send)  # lifespan only
        client = scope.get("client") or (None, None)
        ok, reason = gate_decision(client[0], scope.get("headers") or [], CONFIG.get("remote_clients") or [])
        if ok:
            return await self.inner(scope, receive, send)
        xff = next((v.decode("latin-1") for k, v in (scope.get("headers") or []) if k.lower() == b"x-forwarded-for"), "-")
        # repr() so a crafted path or header cannot forge log lines or escape codes.
        print(f"[acc] refused: {reason} from={xff[:64]!r} path={scope.get('path', '')[:80]!r}", flush=True)
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": b"Forbidden"})


app.add_middleware(RemoteGate)

# proxy_headers=False matters: uvicorn otherwise rewrites the client address
# from X-Forwarded-For for loopback peers, and the gate must see the real
# socket peer (always loopback here) to tell a proxied request from a local one.
UVICORN_OPTIONS = {"log_level": "warning", "reload": False, "proxy_headers": False}


@app.get("/apple-touch-icon.png")
def touch_icon() -> FileResponse:
    """The home-screen icon, when touch_icon points at a PNG on disk."""
    path = CONFIG.get("touch_icon")
    if not path:
        raise HTTPException(status_code=404, detail="No touch icon configured.")
    p = pathlib.Path(path).expanduser()
    if p.suffix.lower() != ".png" or not p.is_file():
        raise HTTPException(status_code=404, detail="Touch icon not found.")
    return FileResponse(str(p), media_type="image/png")
STATIC_DIR = pathlib.Path(__file__).parent / "static"


@app.get("/api/events")
def api_events() -> JSONResponse:
    """Return all parsed events from every configured source."""
    return JSONResponse(content=parse_all_events(CONFIG["event_files"]))


_BOOTTIME = re.compile(r"sec\s*=\s*(\d+)")


def boot_time() -> Optional[float]:
    """The machine's last boot, epoch seconds: macOS kern.boottime, Linux btime; else None."""
    try:
        import subprocess
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True,
                             text=True, timeout=2).stdout
        m = _BOOTTIME.search(out or "")
        if m:
            return float(m.group(1))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        for line in pathlib.Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def project_names() -> dict:
    """The config's project_names, keeping only short text keys and names."""
    raw = CONFIG.get("project_names")
    if not isinstance(raw, dict):
        return {}
    return {k[:200]: v[:40] for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}


@app.get("/api/config")
def api_config() -> JSONResponse:
    """Return only the safe config fields the frontend needs to render itself."""
    mode = CONFIG.get("actions_mode", "readonly")
    payload = {
        "title": CONFIG.get("title", "Agent Command Center"),
        "logo": CONFIG.get("logo"),
        "billing_mode": CONFIG.get("billing_mode", "max"),
        "usage": CONFIG.get("usage", _DEFAULTS["usage"]),
        "actions_mode": mode,
        "content_mode": CONFIG.get("content_mode", "inline"),
        "telegram_configured": _telegram_configured(),
        # When this machine last started (epoch seconds), so the page can tell
        # sessions cut off by a restart from sessions that are still running.
        "booted_at": boot_time(),
        "project_names": project_names(),
    }
    # Only browser-acting modes need the write-back token, and only the
    # same-origin page can read it (cross-origin sites cannot read this response).
    if mode in _BROWSER_ACTION_MODES:
        payload["actions_token"] = ACTIONS_TOKEN
    return JSONResponse(content=payload)


# ── Go offline in N minutes ─────────────────────────────────────────────────────
#
# The "Go offline in…" button writes one small file beside the event file:
# {"at": <epoch seconds>, "set_at": <epoch seconds>, "minutes": N}. The Claude
# Code hook reads it (opt-in, ACC_PAUSE_NOTICE=1) and tells each session, in a
# fixed sentence, to save its work and end its turn by then. Nothing here
# drives an agent, so the endpoint is open in every actions_mode; it still
# requires a custom request header, which makes a browser send a CORS
# preflight that another origin cannot pass. A pause more than two hours past
# reads as none.

_PAUSE_MAX_MINUTES = 12 * 60
_PAUSE_GRACE_SECONDS = 2 * 3600
_PAUSE_HEADER = "x-acc-pause"


def pause_path() -> pathlib.Path:
    return pathlib.Path(CONFIG["event_file"]).expanduser().parent / "pause.json"


def read_pause(now: Optional[float] = None) -> dict:
    """The scheduled pause as the pages see it: {"at": epoch seconds or None, ...}."""
    now = time.time() if now is None else now
    try:
        data = json.loads(pause_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": None}
    at = data.get("at") if isinstance(data, dict) else None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or now > at + _PAUSE_GRACE_SECONDS:
        return {"at": None}
    out: dict = {"at": float(at)}
    if isinstance(data.get("set_at"), (int, float)) and not isinstance(data.get("set_at"), bool):
        out["set_at"] = float(data["set_at"])
    if isinstance(data.get("minutes"), int) and not isinstance(data.get("minutes"), bool):
        out["minutes"] = data["minutes"]
    return out


def pause_status(now: Optional[float] = None) -> dict:
    """What /api/pause returns: the scheduled pause, plus a fresh resume request when there is one."""
    now = time.time() if now is None else now
    out = read_pause(now)
    resume = read_resume(now)
    if resume is not None:
        out["resume"] = resume
    return out


def write_pause(minutes: int, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    path = pause_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": now + minutes * 60, "set_at": now, "minutes": minutes}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)
    return read_pause(now)


def clear_pause() -> dict:
    try:
        pause_path().unlink()
    except FileNotFoundError:
        pass
    return {"at": None}


@app.get("/api/pause")
def api_pause_get() -> JSONResponse:
    return JSONResponse(content=pause_status())


@app.post("/api/pause")
async def api_pause_set(request: Request) -> JSONResponse:
    """Schedule a pause ({"minutes": N}) or cancel it ({"minutes": 0} or {"cancel": true})."""
    if request.headers.get(_PAUSE_HEADER) != "1":
        raise HTTPException(status_code=400, detail="Missing X-ACC-Pause header.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    if body.get("cancel") is True:
        return JSONResponse(content=clear_pause())
    minutes = body.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise HTTPException(status_code=400, detail="minutes (whole number) is required.")
    if minutes == 0:
        return JSONResponse(content=clear_pause())
    if minutes < 1 or minutes > _PAUSE_MAX_MINUTES:
        raise HTTPException(status_code=400, detail="minutes must be between 1 and %d." % _PAUSE_MAX_MINUTES)
    return JSONResponse(content=write_pause(minutes))


# ── Resume all ──────────────────────────────────────────────────────────────────
#
# Back at the keyboard, "Resume all" ends the pause and asks for every session
# that stopped for it to be started again. The dashboard cannot start a turn in
# a Claude Code session by itself; a Claude Code session can (the desktop app
# lets one session message another, which starts the target's turn). So the
# button writes resume.json beside the event file and removes pause.json; a
# standing "bridge" session waits on that file, reads /api/paused for the
# sessions that stopped, messages each one, and reports back through
# /api/resume/done. The hook, meanwhile, tells any session it had warned that
# the user is back. See examples/watchtower-bridge/SKILL.md.

_RESUME_SHOWN_SECONDS = 2 * 3600


def resume_path() -> pathlib.Path:
    return pathlib.Path(CONFIG["event_file"]).expanduser().parent / "resume.json"


def _number(value) -> Optional[float]:
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)


def read_resume(now: Optional[float] = None) -> Optional[dict]:
    """The latest resume request, or None when there is none or it is hours old."""
    now = time.time() if now is None else now
    try:
        data = json.loads(resume_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    at = _number(data.get("at")) if isinstance(data, dict) else None
    if at is None or now > at + _RESUME_SHOWN_SECONDS:
        return None
    out: dict = {"at": at}
    for key in ("pause_at", "since", "done_at"):
        value = _number(data.get(key))
        if value is not None:
            out[key] = value
    woken = data.get("woken")
    if isinstance(woken, int) and not isinstance(woken, bool) and woken >= 0:
        out["woken"] = woken
    return out


def _write_resume_file(record: dict) -> None:
    path = resume_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)


def write_resume(now: Optional[float] = None) -> dict:
    """Record the resume request, remembering which pause it ends, and end that pause."""
    now = time.time() if now is None else now
    pause = read_pause(now)
    record: dict = {"at": now}
    if pause.get("at") is not None:
        record["pause_at"] = pause["at"]
        record["since"] = pause.get("set_at", pause["at"])
    else:
        record["since"] = now - 24 * 3600
    _write_resume_file(record)
    clear_pause()
    return read_resume(now) or record


def mark_resume_done(woken: int, now: Optional[float] = None) -> Optional[dict]:
    now = time.time() if now is None else now
    current = read_resume(now)
    if current is None:
        return None
    current.update({"done_at": now, "woken": woken})
    _write_resume_file(current)
    return read_resume(now)


def _event_time(e: dict) -> Optional[float]:
    ts = e.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def paused_sessions(events: list, links: dict, since: float) -> list:
    """
    Sessions that were told to stop for the pause set at `since` and whose turn
    then ended: told (a "Time limit set" mark with pause true, at or after
    `since`), and nothing since but the turn ending ("Your turn"). A session
    that was cleared, closed, reopened or messaged since is left out. Each row:
    the 8-character session id, the desktop app's id and link when known, the
    project folder, the title, and when it stopped.
    """
    rows: dict = {}
    for e in sorted((e for e in events if isinstance(e, dict)), key=lambda e: e.get("ts") or ""):
        sid = e.get("session")
        if not isinstance(sid, str) or not sid or e.get("source") != "claude-code":
            continue
        t = _event_time(e)
        if t is None:
            continue
        row = rows.setdefault(sid, {"session": sid, "told": None, "stopped": None, "agent": None, "title": None})
        if e.get("agent"):
            row["agent"] = e["agent"]
        if e.get("title"):
            row["title"] = e["title"]
        action, status = e.get("action"), e.get("status")
        if action == "Time limit set" and e.get("pause") is True and t >= since:
            row["told"], row["stopped"] = t, None
        elif row["told"] is None:
            continue
        elif status == "finished":
            row["stopped"] = t
        elif action == "Time limit cleared" or action in ("Session closed", "Session opened", "Session resumed") \
                or status == "started":
            row["told"], row["stopped"] = None, None
    out = []
    for sid, row in rows.items():
        if row["told"] is None or row["stopped"] is None:
            continue
        link = links.get(sid) if isinstance(links, dict) else None
        out.append({
            "session": sid,
            "app": link.get("app") if isinstance(link, dict) else None,
            "web": link.get("web") if isinstance(link, dict) else None,
            "project": row["agent"],
            "title": row["title"],
            "stopped_at": row["stopped"],
        })
    out.sort(key=lambda r: r["stopped_at"])
    return out


@app.get("/api/paused")
def api_paused() -> JSONResponse:
    """The sessions that stopped for the current (or just-ended) pause, for the bridge."""
    now = time.time()
    resume = read_resume(now)
    pause = read_pause(now)
    if resume is not None and resume.get("since") is not None:
        since = resume["since"]
    elif pause.get("at") is not None:
        since = pause.get("set_at", pause["at"])
    else:
        since = now - 24 * 3600
    links = claude_app_links() if CONFIG.get("claude_app_links") else {}
    rows = paused_sessions(parse_all_events(CONFIG["event_files"]), links, since)
    return JSONResponse(content={"since": since, "sessions": rows})


@app.post("/api/resume")
async def api_resume(request: Request) -> JSONResponse:
    """End the pause and ask the bridge to start every session that stopped for it."""
    if request.headers.get(_PAUSE_HEADER) != "1":
        raise HTTPException(status_code=400, detail="Missing X-ACC-Pause header.")
    write_resume()
    return JSONResponse(content=pause_status())


@app.post("/api/resume/done")
async def api_resume_done(request: Request) -> JSONResponse:
    """The bridge reports how many sessions it started."""
    if request.headers.get(_PAUSE_HEADER) != "1":
        raise HTTPException(status_code=400, detail="Missing X-ACC-Pause header.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    woken = body.get("woken") if isinstance(body, dict) else None
    if isinstance(woken, bool) or not isinstance(woken, int) or woken < 0:
        raise HTTPException(status_code=400, detail="woken (whole number, 0 or more) is required.")
    if mark_resume_done(woken) is None:
        raise HTTPException(status_code=409, detail="No resume request is open.")
    return JSONResponse(content=pause_status())


# ── Claude desktop app links ────────────────────────────────────────────────────
#
# The desktop app keeps one small JSON record per Code session. Two of its
# fields tie the app's session to the Claude Code session our hook reports:
# `sessionId` ("local_<uuid>", the app's id) and `cliSessionId` (Claude Code's
# id, whose first 8 characters are the event stream's `session`). A third,
# `bridgeSessionIds`, holds the session's Remote Control id ("session_<id>")
# when Remote Control is on; it gives a claude.ai link that opens the same
# session on a phone or another computer. Three more fields feed the focus view:
# `lastFocusedAt` (when the app last showed the session, epoch ms), `model` and
# `effort`; `isArchived` lets the pages drop a session the app has archived, since
# archiving sends no stop event. Nothing else is read or returned: no title,
# folder, prompt or text.

APP_SESSIONS_DIR = (
    pathlib.Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
)
_APP_ID = re.compile(r"^local_[0-9a-f-]{8,64}$")
_BRIDGE_ID = re.compile(r"^session_[A-Za-z0-9]{10,64}$")
_PLAIN_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,39}$")  # a model or effort name
_APP_RECORD_MAX_BYTES = 2_000_000
# Short, so the focus view notices a switch between sessions within seconds.
_LINKS_TTL_SECONDS = 5.0
_links_cache: dict = {"at": 0.0, "root": None, "links": {}}
# Per record: path -> ((mtime_ns, size), (key, link) or None). The app's records
# run to hundreds of kilobytes each and most never change, so a record is
# parsed again only when its size or modification time moves; an unchanged
# pass costs one stat per record.
_record_cache: dict = {}


def _plain_word(value) -> Optional[str]:
    return value if isinstance(value, str) and _PLAIN_WORD.match(value) else None


def _epoch_ms(value) -> Optional[int]:
    """An epoch-milliseconds stamp between 2001 and 2286, else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 10**12 <= value < 10**13 else None


def claude_app_links(root: Optional[pathlib.Path] = None, now: Optional[float] = None) -> dict:
    """
    Map the event stream's 8-character session id to its app record's facts:
    {"app": "claude://...", "web": "https://claude.ai/code/session_..." or None,
     "focused_at": epoch ms or None, "model": str or None, "effort": str or None,
     "archived": bool}.
    """
    root = pathlib.Path(root) if root is not None else APP_SESSIONS_DIR
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    if _links_cache["root"] == str(root) and now - _links_cache["at"] < _LINKS_TTL_SECONDS:
        return _links_cache["links"]
    links: dict = {}
    newest: dict = {}
    seen: set = set()
    try:
        records = list(root.glob("*/*/local_*.json"))
    except OSError:
        records = []
    for path in records:
        try:
            st = path.stat()
        except OSError:
            continue
        name = str(path)
        seen.add(name)
        signature = (st.st_mtime_ns, st.st_size)
        cached = _record_cache.get(name)
        if cached is not None and cached[0] == signature:
            entry = cached[1]
        else:
            entry = _read_app_record(path, st.st_size)
            _record_cache[name] = (signature, entry)
        if entry is None:
            continue
        key, link = entry
        # Two app records for one Claude Code session (a fork): keep the newest.
        if key in newest and newest[key] >= st.st_mtime:
            continue
        newest[key] = st.st_mtime
        links[key] = dict(link)
    for name in [n for n in _record_cache if n not in seen]:
        del _record_cache[name]
    _links_cache.update({"at": now, "root": str(root), "links": links})
    return links


def _read_app_record(path: pathlib.Path, size: int):
    """(key, link) from one app record, or None when it is not one we can use."""
    if size > _APP_RECORD_MAX_BYTES:
        return None
    try:
        with path.open(encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    app_id, cli_id = rec.get("sessionId"), rec.get("cliSessionId")
    if not (isinstance(app_id, str) and _APP_ID.match(app_id)):
        return None
    if not (isinstance(cli_id, str) and len(cli_id) >= 8):
        return None
    bridge = rec.get("bridgeSessionIds")
    # The newest Remote Control id is the last in the list.
    web_ids = [b for b in bridge if isinstance(b, str) and _BRIDGE_ID.match(b)] if isinstance(bridge, list) else []
    return cli_id[:8], {
        "app": "claude://claude.ai/epitaxy/" + app_id,
        "web": ("https://claude.ai/code/" + web_ids[-1]) if web_ids else None,
        "focused_at": _epoch_ms(rec.get("lastFocusedAt")),
        "model": _plain_word(rec.get("model")),
        "effort": _plain_word(rec.get("effort")),
        "archived": rec.get("isArchived") is True,
    }


@app.get("/api/links")
def api_links() -> JSONResponse:
    """Session id (8 chars) -> its links and, for the focus view, when the app last showed it."""
    if not CONFIG.get("claude_app_links"):
        return JSONResponse(content={})
    return JSONResponse(content=claude_app_links())


# ── Turn summaries (opt-in) ─────────────────────────────────────────────────────
#
# The hook, with ACC_SUMMARIES=1, keeps one <session>.summary.json per session:
# step names plus the end-of-turn recap, question and button labels, already
# scrubbed and capped. This reads them back and re-checks every field (types and
# caps), so a hand-edited or foreign file can add nothing else. Files older than
# 7 days are skipped (the hook deletes them).

_SUMMARY_KEEP_SECONDS = 7 * 86400
_SUMMARY_MAX_BYTES = 64_000
_SUMMARY_TTL_SECONDS = 5.0
_STEP_STATES = ("pending", "in_progress", "completed")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")  # the hook's _now_iso()
_summaries_cache: dict = {"at": 0.0, "root": None, "data": {}}


def summaries_root() -> pathlib.Path:
    configured = CONFIG.get("summaries_dir")
    if isinstance(configured, str) and configured.strip():
        return pathlib.Path(configured).expanduser()
    return pathlib.Path(CONFIG["event_file"]).expanduser().parent / "state"


def _text(value, cap: int) -> Optional[str]:
    return value[:cap] if isinstance(value, str) and value.strip() else None


def _stamp(value) -> Optional[str]:
    return value if isinstance(value, str) and _STAMP.fullmatch(value) else None


def _clean_summary(doc: dict) -> dict:
    steps = []
    for x in doc.get("steps") if isinstance(doc.get("steps"), list) else []:
        if isinstance(x, dict) and len(steps) < 12:
            status = x.get("status") if x.get("status") in _STEP_STATES else "pending"
            steps.append({"name": _text(x.get("name"), 60) or "", "status": status})
    total = doc.get("steps_total")
    if isinstance(total, bool) or not isinstance(total, int) or not len(steps) <= total <= 999:
        total = len(steps)
    options = [o[:24] for o in (doc.get("options") if isinstance(doc.get("options"), list) else [])
               if isinstance(o, str) and o.strip()][:2]
    return {
        "steps": steps,
        "steps_total": total,
        "recap": _text(doc.get("recap"), 300),
        "ask": _text(doc.get("ask"), 100),
        "options": options,
        "blocked": doc.get("blocked") is True,
        "reply": _text(doc.get("reply"), 1500),
        "turn_at": _stamp(doc.get("turn_at")),
        "updated": _stamp(doc.get("updated")),
    }


def turn_summaries(root: Optional[pathlib.Path] = None, now: Optional[float] = None) -> dict:
    """Map the event stream's 8-character session id to its latest turn summary."""
    root = pathlib.Path(root) if root is not None else summaries_root()
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    out: dict = {}
    newest: dict = {}
    try:
        paths = list(root.glob("*.summary.json"))
    except OSError:
        return out
    for path in paths:
        try:
            st = path.stat()
            if st.st_size > _SUMMARY_MAX_BYTES or now - st.st_mtime > _SUMMARY_KEEP_SECONDS:
                continue
            with path.open(encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError, RecursionError):  # a deeply nested file is a RecursionError
            continue
        if not isinstance(doc, dict):
            continue
        key = doc.get("session")
        if not (isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,8}", key)):
            continue
        if key in newest and newest[key] >= st.st_mtime:
            continue
        newest[key] = st.st_mtime
        out[key] = _clean_summary(doc)
    return out


@app.get("/api/summaries")
def api_summaries() -> JSONResponse:
    """Session id (8 chars) -> its latest turn summary, when turn_summaries is on."""
    if not CONFIG.get("turn_summaries"):
        return JSONResponse(content={})
    now = datetime.now(timezone.utc).timestamp()
    root = str(summaries_root())
    if _summaries_cache["root"] != root or now - _summaries_cache["at"] >= _SUMMARY_TTL_SECONDS:
        _summaries_cache.update({"at": now, "root": root, "data": turn_summaries(pathlib.Path(root), now)})
    return JSONResponse(content=_summaries_cache["data"])


# ── Reviewable content (reference mode) ──────────────────────────────────────


def _content_root() -> pathlib.Path:
    """Sandbox root for reference-mode draft files."""
    cd = CONFIG.get("content_dir")
    if cd:
        return pathlib.Path(cd).expanduser().resolve()
    return pathlib.Path(CONFIG["event_file"]).expanduser().resolve().parent


def _resolve_ref_path(ref: str) -> pathlib.Path:
    """
    Resolve a content_ref path against the content root, refusing anything that
    escapes it (``..`` or absolute paths pointing elsewhere).
    """
    root = _content_root()
    candidate = (root / ref).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(
            status_code=403,
            detail="Referenced path is outside the content directory.",
        )
    return candidate


@app.get("/api/content")
def api_content(ref: str = "") -> JSONResponse:
    """Return the text of a referenced draft (reference content mode)."""
    if not ref:
        raise HTTPException(status_code=400, detail="ref query parameter is required.")
    path = _resolve_ref_path(ref)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Referenced content not found.")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(status_code=500, detail="Could not read referenced content.")
    return JSONResponse(content={"ref": ref, "text": text})


# ── Correction log (the training signal) ─────────────────────────────────────
#
# When a reviewer Revises + Submits, we record the ORIGINAL draft (or its
# reference + a content hash) plus a structured, human-legible diff — never the
# full revised copy, which is reconstructable from original + diff. This is the
# dashboard's own logic (not the self-hoster stub) and is written to a separate
# corrections.jsonl, kept out of the event stream and out of git.

_PARA_SPLIT = re.compile(r"\n\s*\n")


def _paragraphs(text: str) -> list[str]:
    """Split a draft into non-empty paragraphs on blank-line boundaries."""
    return [p.strip() for p in _PARA_SPLIT.split((text or "").strip()) if p.strip()]


def _inline_blocks(content) -> list[tuple[str, str]]:
    """Normalise an inline `content` array into (label, text) pairs."""
    blocks: list[tuple[str, str]] = []
    if isinstance(content, list):
        for i, item in enumerate(content):
            if isinstance(item, dict):
                text = str(item.get("text") or "")
                label = str(item.get("label") or f"Block {i + 1}")
            else:
                text, label = str(item), f"Block {i + 1}"
            blocks.append((label, text.strip()))
    return blocks


def _assemble_inline_text(blocks: list[tuple[str, str]]) -> str:
    """Join block texts into the same editable draft the browser shows."""
    return "\n\n".join(text for _, text in blocks if text)


def _inline_paragraph_labels(blocks: list[tuple[str, str]]) -> list[str]:
    """Label each paragraph (in order) with the block it belongs to."""
    labels: list[str] = []
    for label, text in blocks:
        for _ in _paragraphs(text):
            labels.append(label)
    return labels


def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _structured_diff(
    orig_paras: list[str], rev_paras: list[str], labels: list[str]
) -> list[dict]:
    """
    Produce human-legible, reconstructable change records at paragraph level:
    which block changed from → to. Not a raw patch blob.
    """
    def lbl(i: int) -> str:
        return labels[i] if 0 <= i < len(labels) else f"Paragraph {i + 1}"

    changes: list[dict] = []
    sm = difflib.SequenceMatcher(a=orig_paras, b=rev_paras, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            for k in range(max(i2 - i1, j2 - j1)):
                oi, oj = i1 + k, j1 + k
                frm = orig_paras[oi] if oi < i2 else None
                to = rev_paras[oj] if oj < j2 else None
                if frm is not None and to is not None:
                    changes.append({"op": "modified", "block": lbl(oi), "from": frm, "to": to})
                elif to is not None:
                    changes.append({"op": "added", "block": lbl(i2 - 1), "from": None, "to": to})
                else:
                    changes.append({"op": "removed", "block": lbl(oi), "from": frm, "to": None})
        elif tag == "delete":
            for oi in range(i1, i2):
                changes.append({"op": "removed", "block": lbl(oi), "from": orig_paras[oi], "to": None})
        elif tag == "insert":
            anchor = "start" if i1 == 0 else f"after {lbl(i1 - 1)}"
            for oj in range(j1, j2):
                changes.append({"op": "added", "block": anchor, "from": None, "to": rev_paras[oj]})
    return changes


def _find_awaiting_approval(approval_id: str) -> Optional[dict]:
    """Latest awaiting_approval event for an approval_id, from the event stream."""
    match: Optional[dict] = None
    for e in parse_all_events(CONFIG["event_files"]):
        if e.get("approval_id") == approval_id and e.get("status") == "awaiting_approval":
            match = e
    return match


def _write_correction(
    *, approval_id: str, agent: Optional[str], revised_text: str, reason: Optional[str]
) -> dict:
    """
    Append one correction record to corrections_file. Returns a small summary
    dict for the API response. Original content/category come from the
    authoritative event stream, not the client.
    """
    event = _find_awaiting_approval(approval_id) or {}
    category = event.get("category")
    ev_agent = event.get("agent") or agent
    content = event.get("content")
    content_ref = event.get("content_ref")

    if content_ref:
        mode = "reference"
        ref_path = content_ref.get("path") if isinstance(content_ref, dict) else content_ref
        original_text = ""
        if ref_path:
            try:
                p = _resolve_ref_path(str(ref_path))
                if p.is_file():
                    original_text = p.read_text(encoding="utf-8")
            except Exception:
                original_text = ""
        orig_paras = _paragraphs(original_text)
        labels = [f"Paragraph {i + 1}" for i in range(len(orig_paras))]
    else:
        mode = "inline"
        blocks = _inline_blocks(content)
        original_text = _assemble_inline_text(blocks)
        orig_paras = _paragraphs(original_text)
        labels = _inline_paragraph_labels(blocks)

    diff = _structured_diff(orig_paras, _paragraphs(revised_text), labels)

    record: dict = {
        "schema": "acc-correction/1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": ev_agent,
        "approval_id": approval_id,
        "content_mode": mode,
        "original_hash": _content_hash(original_text),
        "diff": diff,
    }
    if category:
        record["category"] = category
    if content_ref:
        record["original_ref"] = content_ref  # keep the log lean; not the full text
    else:
        record["original"] = original_text
    # Reason is truly optional: omit the key entirely when blank.
    if reason and reason.strip():
        record["reason"] = reason.strip()

    path = pathlib.Path(CONFIG.get("corrections_file", "./corrections.jsonl"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(f"[acc] correction logged: approval_id={approval_id!r} changes={len(diff)} → {path}")
    return {"correction_logged": True, "changes": len(diff), "corrections_file": str(path)}


# ── Browser-mode write-back ──────────────────────────────────────────────────
#
# ┌────────────────────────────────────────────────────────────────────────────┐
# │  SELF-HOSTER ACTION HOOK — STUB (intentionally inert in this public repo)    │
# │                                                                              │
# │  This function performs NO real agent action. The public project ships the   │
# │  mechanism (an authenticated endpoint + a place to plug in), not anyone's    │
# │  live agent control. Wire your own system in here: enqueue the decision,     │
# │  call your orchestrator, append a resolving event to your stream, message    │
# │  your control channel, etc. Whatever dict you return is sent to the browser. │
# └────────────────────────────────────────────────────────────────────────────┘
def handle_action(
    *,
    approval_id: str,
    decision: str,
    revised_text: Optional[str] = None,
    agent: Optional[str] = None,
) -> dict:
    """Stub action handler. Replace the body with your own integration."""
    print(
        f"[acc] (stub) action received: decision={decision!r} "
        f"approval_id={approval_id!r} agent={agent!r} "
        f"revised={'yes' if revised_text else 'no'}"
    )
    return {
        "ok": True,
        "stub": True,
        "decision": decision,
        "approval_id": approval_id,
        "message": (
            "Received by the dashboard, but no action handler is wired in this "
            "build. Implement handle_action() in server.py to connect your own "
            "agent system."
        ),
    }


@app.post("/api/actions")
async def api_actions(
    request: Request,
    x_acc_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """
    Authenticated write-back endpoint for browser action modes.

    Bound to the same interface as the dashboard (never 0.0.0.0 unless you set
    that yourself). Only active when actions_mode is "browser" or "both".
    Requires the X-ACC-Token header — a custom header, so browsers force a CORS
    preflight that other origins cannot pass, giving CSRF protection on top of
    the shared secret. On a "revise" decision the dashboard logs a correction
    record (real logic); the actual agent action is delegated to handle_action()
    (a stub here).
    """
    if CONFIG.get("actions_mode") not in _BROWSER_ACTION_MODES:
        raise HTTPException(
            status_code=403,
            detail="Browser actions are disabled (actions_mode is not 'browser' or 'both').",
        )
    if not x_acc_token or not secrets.compare_digest(x_acc_token, ACTIONS_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid action token.")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    approval_id = body.get("approval_id")
    decision = body.get("decision")
    if not approval_id or not isinstance(approval_id, str):
        raise HTTPException(status_code=400, detail="approval_id (string) is required.")
    if decision not in {"approve", "revise", "decline"}:
        raise HTTPException(
            status_code=400,
            detail="decision must be one of: approve, revise, decline.",
        )

    revised_text = body.get("revised_text")
    if revised_text is not None and not isinstance(revised_text, str):
        raise HTTPException(status_code=400, detail="revised_text must be a string.")
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=400, detail="reason must be a string.")
    agent = body.get("agent") if isinstance(body.get("agent"), str) else None

    # Revise → log the correction (original + diff + optional reason) separately.
    correction: dict = {}
    if decision == "revise":
        if revised_text is None:
            raise HTTPException(
                status_code=400, detail="revised_text is required for a revise decision."
            )
        try:
            correction = _write_correction(
                approval_id=approval_id,
                agent=agent,
                revised_text=revised_text,
                reason=reason,
            )
        except HTTPException:
            raise
        except Exception as exc:
            correction = {"correction_logged": False, "error": str(exc)}

    result = handle_action(
        approval_id=approval_id,
        decision=decision,
        revised_text=revised_text,
        agent=agent,
    )
    result.update(correction)
    return JSONResponse(content=result)


# ── Direct-the-agent prompt (browser action modes) ───────────────────────────
#
# Same "mechanism, not live control" stance as handle_action: this stub performs
# NO real dispatch. Wire your own system in here to actually queue the prompt for
# the agent (enqueue a job, call your orchestrator, etc.).
def handle_prompt(*, agent: Optional[str], prompt: str) -> dict:
    """Stub prompt handler. Replace the body with your own integration."""
    print(f"[acc] (stub) prompt received: agent={agent!r} chars={len(prompt)}")
    return {
        "ok": True,
        "stub": True,
        "agent": agent,
        "message": (
            "Prompt received by the dashboard, but no prompt handler is wired in "
            "this build. Implement handle_prompt() in server.py to queue it for "
            "your agent."
        ),
    }


@app.post("/api/prompt")
async def api_prompt(
    request: Request,
    x_acc_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Authenticated endpoint to queue a free-text prompt for an agent."""
    if CONFIG.get("actions_mode") not in _BROWSER_ACTION_MODES:
        raise HTTPException(
            status_code=403,
            detail="Prompting is disabled (actions_mode is not 'browser' or 'both').",
        )
    if not x_acc_token or not secrets.compare_digest(x_acc_token, ACTIONS_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid action token.")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    prompt = body.get("prompt")
    if not prompt or not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt (non-empty string) is required.")
    agent = body.get("agent") if isinstance(body.get("agent"), str) else None

    return JSONResponse(content=handle_prompt(agent=agent, prompt=prompt.strip()))


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    """
    Server-Sent Events endpoint. Emits the full event list whenever any
    source file changes, plus a keepalive comment every 2 seconds.
    """
    event_files = CONFIG["event_files"]

    async def generator() -> AsyncIterator[str]:
        last_sizes: Optional[tuple] = None
        while True:
            if await request.is_disconnected():
                break
            sizes = _sizes(event_files)
            if sizes != last_sizes:
                last_sizes = sizes
                events = parse_all_events(event_files)
                payload = json.dumps(events)
                yield f"data: {payload}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# Serve the SPA and static assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def page_for(path: str) -> str:
    """The static page a browser path maps to: /classic, /sessions, /focus, else the default view."""
    first = path.strip("/").split("/", 1)[0]
    if first in _VIEWS:
        return _VIEWS[first]
    return _VIEWS.get(CONFIG.get("view", "sessions"), "sessions.html")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str) -> FileResponse:
    page = STATIC_DIR / page_for(full_path)
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(status_code=404, detail=f"{page.name} not found in static/")


# ── Optional Telegram relay ────────────────────────────────────────────────────


def _maybe_start_telegram() -> None:
    tg = CONFIG.get("telegram")
    if not tg or not isinstance(tg, dict):
        return
    token = tg.get("bot_token", "")
    chat_id = tg.get("chat_id", "")
    if not token or not chat_id:
        return
    try:
        from telegram_relay import TelegramRelay
        base_url = tg.get(
            "dashboard_url",
            f"http://127.0.0.1:{CONFIG['port']}",
        )
        relay = TelegramRelay(bot_token=token, chat_id=chat_id, dashboard_url=base_url)
        relay.start()
        print("[acc] Telegram relay started.")
    except ImportError:
        print("[acc] telegram_relay.py not found — Telegram feature disabled.")
    except Exception as exc:
        print(f"[acc] Telegram relay failed to start: {exc}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = CONFIG["bind"]
    port = int(CONFIG["port"])
    mode = CONFIG.get("actions_mode", "readonly")
    print(f"[acc] Agent Command Center")
    for i, event_file in enumerate(CONFIG["event_files"]):
        label = "Event file  " if i == 0 else "            "
        print(f"[acc] {label}: {pathlib.Path(event_file).resolve()}")
    print(f"[acc] Dashboard   : http://{host}:{port}")
    print(f"[acc] Action mode : {mode}")
    print(f"[acc] Content mode: {CONFIG.get('content_mode', 'inline')}")
    if mode in _BROWSER_ACTION_MODES:
        print(f"[acc]   In-browser Approve/Revise/Decline is ENABLED. This can act on")
        print(f"[acc]   your agents — keep it behind a private mesh and single-user access.")
        if not CONFIG.get("actions_token"):
            print(f"[acc]   Auto-generated action token (this run): {ACTIONS_TOKEN}")
    if mode in {"telegram", "both"} and not _telegram_configured():
        print(f"[acc]   Warning: actions_mode is {mode!r} but no telegram block is configured.")
    print(f"[acc] Remote access: use a private mesh (e.g. Tailscale), not port-forwarding.")
    _maybe_start_telegram()
    uvicorn.run("server:app", host=host, port=port, **UVICORN_OPTIONS)
