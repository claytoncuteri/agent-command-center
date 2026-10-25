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
    # Which page "/" serves: "sessions" (one row per session, needs-you first)
    # or "classic" (agent cards and the approvals workflow). Both stay reachable
    # at /sessions and /classic.
    "view": "sessions",
    # macOS + Claude desktop app only: link each Claude Code session row to the
    # same session in the app ("Open in Claude"). Reads only two id fields from
    # the app's per-session records. Off unless you turn it on.
    "claude_app_links": False,
    # Remote viewing through Tailscale serve (or any local reverse proxy).
    # A request that arrives with proxy headers is "remote" and is allowed only
    # when its X-Forwarded-For is one of these exact addresses. Empty = no
    # remote viewers (fail closed). Direct local requests are unaffected.
    "remote_clients": [],
    # Optional PNG served at /apple-touch-icon.png (the iPhone home-screen icon).
    "touch_icon": None,
}

_VIEWS = {"sessions": "sessions.html", "classic": "index.html"}

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
    }
    # Only browser-acting modes need the write-back token, and only the
    # same-origin page can read it (cross-origin sites cannot read this response).
    if mode in _BROWSER_ACTION_MODES:
        payload["actions_token"] = ACTIONS_TOKEN
    return JSONResponse(content=payload)


# ── Claude desktop app links ────────────────────────────────────────────────────
#
# The desktop app keeps one small JSON record per Code session. Two of its
# fields tie the app's session to the Claude Code session our hook reports:
# `sessionId` ("local_<uuid>", the app's id) and `cliSessionId` (Claude Code's
# id, whose first 8 characters are the event stream's `session`). A third,
# `bridgeSessionIds`, holds the session's Remote Control id ("session_<id>")
# when Remote Control is on; it gives a claude.ai link that opens the same
# session on a phone or another computer. Nothing else is read or returned.

APP_SESSIONS_DIR = (
    pathlib.Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
)
_APP_ID = re.compile(r"^local_[0-9a-f-]{8,64}$")
_BRIDGE_ID = re.compile(r"^session_[A-Za-z0-9]{10,64}$")
_APP_RECORD_MAX_BYTES = 2_000_000
_LINKS_TTL_SECONDS = 30.0
_links_cache: dict = {"at": 0.0, "root": None, "links": {}}


def claude_app_links(root: Optional[pathlib.Path] = None, now: Optional[float] = None) -> dict:
    """
    Map the event stream's 8-character session id to its links:
    {"app": "claude://...", "web": "https://claude.ai/code/session_..." or None}.
    """
    root = pathlib.Path(root) if root is not None else APP_SESSIONS_DIR
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    if _links_cache["root"] == str(root) and now - _links_cache["at"] < _LINKS_TTL_SECONDS:
        return _links_cache["links"]
    links: dict = {}
    newest: dict = {}
    try:
        records = list(root.glob("*/*/local_*.json"))
    except OSError:
        records = []
    for path in records:
        try:
            st = path.stat()
            if st.st_size > _APP_RECORD_MAX_BYTES:
                continue
            with path.open(encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        app_id, cli_id = rec.get("sessionId"), rec.get("cliSessionId")
        if not (isinstance(app_id, str) and _APP_ID.match(app_id)):
            continue
        if not (isinstance(cli_id, str) and len(cli_id) >= 8):
            continue
        key = cli_id[:8]
        # Two app records for one Claude Code session (a fork): keep the newest.
        if key in newest and newest[key] >= st.st_mtime:
            continue
        newest[key] = st.st_mtime
        bridge = rec.get("bridgeSessionIds")
        # The newest Remote Control id is the last in the list.
        web_ids = [b for b in bridge if isinstance(b, str) and _BRIDGE_ID.match(b)] if isinstance(bridge, list) else []
        links[key] = {
            "app": "claude://claude.ai/epitaxy/" + app_id,
            "web": ("https://claude.ai/code/" + web_ids[-1]) if web_ids else None,
        }
    _links_cache.update({"at": now, "root": str(root), "links": links})
    return links


@app.get("/api/links")
def api_links() -> JSONResponse:
    """Session id (8 chars) -> {"app", "web"} links that open it in Claude."""
    if not CONFIG.get("claude_app_links"):
        return JSONResponse(content={})
    return JSONResponse(content=claude_app_links())


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
    """The static page a browser path maps to: /classic, /sessions, else the default view."""
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
