# Agent Command Center

A self-hostable dashboard for multi-agent AI systems.
Any agent stack that emits events conforming to the [contract](CONTRACT.md)
can be visualised here — no code changes required.

**Read-only by default.** Acting on agents from the browser is opt-in and
off out of the box — see [Action modes](#action-modes).

**Dark HUD aesthetic &middot; live event stream &middot; agent cards &middot;
approval tracking &middot; token usage meters &middot; zero JS dependencies**

---

## 60-second quickstart

```bash
# 1. Clone and install
git clone https://github.com/your-org/agent-command-center
cd agent-command-center
pip install -r requirements.txt

# 2. (Optional) copy and edit config
cp config.example.json config.json

# 3. Start the demo emitter in one terminal
python examples/emit_demo.py

# 4. Start the server in another terminal
python server.py

# 5. Open the dashboard
open http://127.0.0.1:7000
```

The dashboard updates live as `emit_demo.py` writes events.
Kill the demo and wire in your own agents (see below).

---

## Config reference

Copy `config.example.json` → `config.json` (gitignored). Edit as needed.

| Key | Default | Notes |
|---|---|---|
| `event_file` | `./events.jsonl` | Path to your append-only NDJSON log. |
| `event_files` | `null` | A list of NDJSON logs merged into one view; overrides `event_file` when set. Missing files are tolerated. |
| `claude_app_links` | `false` | macOS with the Claude desktop app: show **Open in Claude** on sessions that are waiting on you, opening that exact session in the app. Reads only the `sessionId` and `cliSessionId` fields of the app's per-session records. |
| `remote_clients` | `[]` | Tailnet (or LAN) addresses allowed to view the dashboard through a local reverse proxy such as `tailscale serve`. Empty means no remote viewers. See [Viewing from your phone](#viewing-from-your-phone). |
| `touch_icon` | `null` | Path to a PNG served at `/apple-touch-icon.png`, the icon an iPhone uses for Add to Home Screen. |
| `view` | `"sessions"` | What `/` serves: `"sessions"` (one row per session, blocked sessions first) or `"classic"` (agent cards and the browser approvals workflow). Both stay reachable at `/sessions` and `/classic`. |
| `bind` | `127.0.0.1` | Interface to bind. Keep at `127.0.0.1` unless using a private mesh. |
| `port` | `7000` | TCP port. |
| `title` | `"Agent Command Center"` | Displayed in the header and browser tab. |
| `logo` | `null` | Path or `data:` URI for a custom logo. File must not be committed. |
| `billing_mode` | `"max"` | `"max"` = subscription (hides per-run cost); `"api"` = reserved for future cost display. |
| `usage.window_label` | `"SESSION"` | Label shown above usage meters. |
| `usage.reset_label` | `"clears on page reload"` | Shown below each meter bar. |
| `usage.tokens_max` | `null` | If set, meters show % of this ceiling instead of relative %. |
| `actions_mode` | `"readonly"` | How approvals are handled: `"readonly"`, `"browser"`, `"telegram"`, or `"both"`. See [Action modes](#action-modes). |
| `actions_token` | `null` | Browser-acting modes only. Shared secret the browser sends with each decision. `null` auto-generates one per server start (printed on boot). |
| `content_mode` | `"inline"` | How reviewable drafts reach the dashboard: `"inline"` or `"reference"`. See [Content modes](#content-modes). |
| `content_dir` | `null` | Sandbox root for reference-mode draft files. `null` → the directory of `event_file`. |
| `corrections_file` | `./corrections.jsonl` | Where Revise+Submit decisions are logged (separate from the event stream, gitignored). |
| `telegram` | `null` | See Telegram section below. |

### Custom logo

Set `logo` to an image the browser can load: a `data:` URI (most reliable for
a local image) or an `http(s)` URL. A list shows several portraits in the
session view's header, each in a gold ring and joined by a small star; the
classic view uses the first. Keep it in `config.json`, which is never
committed.

```json
{ "logo": ["data:image/jpeg;base64,...", "data:image/png;base64,..."] }
```

To turn a local image into a data URI on macOS:

```bash
printf 'data:image/png;base64,%s' "$(base64 -i ~/Pictures/logo.png)"
```

---

## Action modes

Approvals in the stream (`awaiting_approval` events) show up in the approvals
panel and open a review modal. What that modal lets you *do* is controlled by
one config key, `actions_mode`. Nothing is hardcoded — the browser reads the
mode from the server and renders the matching buttons or note.

| Mode | In-browser buttons | What it does | Use when |
|---|---|---|---|
| `"readonly"` **(default)** | none | Shows the full request and reasoning with a note: *"Read-only view. Enable an action mode in config to approve or edit here."* Cannot trigger anything. | Always safe. The correct default for anyone self-hosting, including exposure you didn't intend. |
| `"browser"` | Approve / Revise / Decline | Decisions POST to an authenticated write-back endpoint on the server. **Revise** opens the copy in an editable field → **Save** returns to the review screen → **Submit** sends the decision. | Single user, behind a private mesh only. This can act on your agents. |
| `"telegram"` | none | Shows the content plus a note that actions happen in the configured Telegram chat. Only meaningful when the `telegram` block is configured. | You already drive approvals from Telegram. |
| `"both"` | Approve / Revise / Decline | Same live in-browser actions as `browser` (same authenticated endpoint + correction log), **plus** a note that the same approvals are actionable from your configured Telegram chat. | You want to act from either the browser or Telegram. Carries the browser-mode security tradeoff. |

### The security tradeoff

**`readonly` is the safe default and the recommended mode.** The dashboard
reads your event log and cannot act on anything — even if the port is reachable
by someone it shouldn't be, there are no buttons that do anything.

**`browser` mode (and `both`) trades that safety for convenience.** It exposes an
endpoint that acts on your agents, so it must stay behind a private mesh (e.g.
[Tailscale](https://tailscale.com)) with single-user access. It is bound to the
same interface as the dashboard (`127.0.0.1` by default — **never `0.0.0.0`**),
and the write-back endpoint is authenticated:

- Every decision must carry the `X-ACC-Token` header. The token comes from
  `actions_token` in config, or is auto-generated per server start (printed on
  boot) and handed only to the same-origin dashboard page.
- Because it is a custom header, browsers force a CORS preflight that other
  origins cannot pass — so a random site you visit cannot forge a decision.

This is defence for a **single-user, private-mesh** deployment. It is **not**
hardening for a multi-user or internet-exposed dashboard. Do not put browser or
both mode on a shared or public host.

### Wiring your own action handler (browser / both mode)

This public repo ships the **mechanism, not anyone's live agent control.** The
write-back endpoint (`POST /api/actions` in `server.py`) authenticates the
request and validates the payload, then calls `handle_action()` — an
intentionally inert **stub** that performs no real agent action and just echoes
back a message.

To make it actually do something, implement `handle_action()` with your own
integration: enqueue the decision, call your orchestrator, append a resolving
event to your stream, message your control channel — whatever your system needs.
It receives `approval_id`, `decision` (`approve` / `revise` / `decline`), the
`revised_text` if the user edited the copy, and the `agent` name; whatever dict
you return is sent back to the browser.

> Note: on a `revise` the dashboard also writes a correction record itself (see
> [Content modes](#content-modes)) — that part is real and does not depend on
> your `handle_action()` implementation.

---

## Content modes

An approval can carry the **actual draft** the reviewer signs off on, so the
approval modal shows the full copy (not just a one-line summary) and **Revise**
edits that copy. How the draft reaches the dashboard is set by `content_mode`:

| Mode | Draft lives in | Use when |
|---|---|---|
| `"inline"` **(default)** | The event's `content` array (`{label, text, path?}` blocks). The full draft travels in the stream. | The simple default. Good for most setups. |
| `"reference"` | A file on disk, pointed to by the event's `content_ref` (`{id?, path}`). The dashboard reads it via `GET /api/content?ref=…`, sandboxed to `content_dir`. | You want a **lean event log** — when agents re-read the log, or when drafts shouldn't sit in telemetry. |

The modal renders whichever field is present, so a single deployment can mix
both. **To switch**, set `content_mode` in `config.json` and have your emitter
produce the matching shape (`content` vs `content_ref`); the bundled
`examples/emit_demo.py` honors `content_mode` and, in reference mode, writes its
sample draft to `drafts/weekly_digest.md`.

### Correction log (the training signal)

When a reviewer **Revises + Submits** (browser/both mode), the dashboard appends
one record to `corrections_file` (default `corrections.jsonl`) — **separate from
the event stream and gitignored**. Each record captures:

- the **original** draft (inline) or its **reference + a content hash** (reference),
- a **structured, human-legible diff** — which block changed *from → to*, not a raw patch,
- an **optional `reason`** (omitted entirely when left blank — never stored as `""`),
- plus `ts`, `agent`, `category`, and `approval_id`.

The full revised copy is **not** stored — the final is reconstructable from the
original + diff. This keeps a clean, reviewable record of every human correction
as a training signal, without duplicating drafts into the event log.

---

## Security

**The dashboard is read-only by default and binds to localhost only.**

- In the default `readonly` mode it only reads `events.jsonl`; it never writes,
  triggers, or calls any agent. Enabling `browser` or `both` mode opts into an
  endpoint that can act on agents — see [Action modes](#action-modes) for the tradeoff.
- Bind address defaults to `127.0.0.1`. Never set it to `0.0.0.0` and
  expose the port to the internet.
- For access from other devices, use a **private mesh** (e.g.
  [Tailscale](https://tailscale.com)) — not port-forwarding or a reverse proxy
  open to the public internet.
- `config.json` is gitignored. It may contain your event-file path and
  (optionally) a Telegram bot token — keep it off version control.
- The default logo slot ships a geometric SVG; your personal logo is never
  committed.

---

## Wire up your own agents

Emit one JSON object per line to an append-only `.jsonl` file.
The only required fields are `ts`, `agent`, `action`, and `status`.

```python
import json, uuid
from datetime import datetime, timezone
from pathlib import Path

def emit(event_file: str, agent: str, action: str, status: str, **kwargs):
    event = {
        "schema": "acc/1",
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "action": action,
        "status": status,
        **kwargs,
    }
    with open(event_file, "a") as f:
        f.write(json.dumps(event) + "\n")

# Example usage
emit("events.jsonl", agent="MY_AGENT", action="Processing batch", status="started")
emit("events.jsonl", agent="MY_AGENT", action="Processing batch", status="finished",
     result="42 items processed", tokens=1800)
```

See [CONTRACT.md](CONTRACT.md) for the full schema including approval flows,
`level: flash`, and all status values.

### Several event files at once

Set `event_files` (a list) instead of `event_file` to merge streams from more
than one source into one dashboard, for example your laptop's Claude Code
sessions plus a server-side agent stack:

```json
"event_files": ["~/.agent-command-center/events.jsonl", "/mnt/agents/events.jsonl"]
```

Paths expand `~`. A missing file is fine; it just contributes nothing until it
appears. The first file is the primary one (it anchors the reference-mode
content sandbox when `content_dir` is unset).

---

## Claude Code sessions

`examples/claude_code_hook.py` turns every [Claude Code](https://claude.com/claude-code)
session into an agent on the dashboard, with no changes to your projects. It
is a Claude Code *hook*: Claude Code runs it at session start, when you send a
prompt, when a tool needs your permission, when a tool finishes, and when the
turn ends, and the script appends one `acc/1` event per moment. Permission
prompts and questions waiting on you show up in the approvals panel; answering
them in Claude Code clears them.

**Privacy by construction.** The hook reads only the event name, the session
id, the working folder, the tool name and two flags. It never reads your
prompt, tool inputs or outputs, the assistant's reply, or the transcript, and
every `detail` is a fixed template (`permission: Bash`). It prints nothing and
always exits 0, so it cannot block or steer a session. See the docstring at the
top of the script for the full event mapping.

**1. Install a pinned copy.** Don't point Claude Code at this working tree: a
`git pull` would change code that runs inside every session. Copy it:

```bash
mkdir -p ~/.claude/hooks
cp examples/claude_code_hook.py ~/.claude/hooks/acc_claude_code_hook.py
shasum -a 256 ~/.claude/hooks/acc_claude_code_hook.py   # keep this for later checks
```

**2. Register the hooks** in `~/.claude/settings.json` (user level, so every
project reports). Merge this into any `hooks` block you already have:

```json
{
  "hooks": {
    "SessionStart":       [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "timeout": 5}]}],
    "UserPromptSubmit":   [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "timeout": 5}]}],
    "PermissionRequest":  [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "timeout": 5}]}],
    "PermissionDenied":   [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "timeout": 5}]}],
    "PreToolUse":         [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "async": true}]}],
    "PostToolUse":        [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "async": true}]}],
    "PostToolUseFailure": [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "async": true}]}],
    "Stop":               [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "timeout": 5}]}],
    "SessionEnd":         [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/acc_claude_code_hook.py\"", "timeout": 5}]}]
  }
}
```

`PreToolUse` and `PostToolUse` fire on every tool call, so they run `async`
(in the background, never delaying the session). `PreToolUse` writes an event
only when the session's activity changes ("Editing files" to "Running a
command"), so a run of edits is one line. The others are quick and run inline
with a short timeout. Python 3.8 or newer is enough; the script has no dependencies.

**3. Point the dashboard at the file.** Events go to
`~/.agent-command-center/events.jsonl` (override with `ACC_EVENT_FILE`). Set
`"event_file": "~/.agent-command-center/events.jsonl"` in `config.json`, or add
it to `event_files` next to your other streams.

**Session titles (opt-in).** Claude Code does not pass a session's title to
hooks. To show titles, prefix each hook command with `ACC_READ_TITLES=1 `: the
script then reads the title from the session's history file, parsing only its
small `custom-title` lines and skipping every other line undecoded, and only
for files under `~/.claude/projects`. Without it, rows read "Session <id>".

**Paused sessions (opt-in).** Prefix each hook command with
`ACC_DETECT_PAUSE=1 ` too, and a message asking a session to pause ("please
pause", "can you pause while I walk through the airport and we pick up later",
"closing my laptop") puts it under **Paused** once it stops, until your next
message.
- **Where it looks:** the hook checks the prompt, and, at the end of each turn and at most every 20 s while the session works, your newest typed message in its history file. That second check catches messages you send mid-turn, which arrive queued and fire no prompt hook.
- **What it writes:** the text is matched in memory and never written; only a `pause` flag is.
- **What it ignores:** talk about a product's own pause feature, such as "pause the video" or "the pause button", and notes from other sessions.

**Step progress.** When a session keeps a task list (the terminal's TodoWrite
or the desktop app's plan), working rows show "Step 3 of 6" with a segment per
step. Only the steps' status values are read, never their text.

**The session view** (`/`, the default) shows one row per session, sorted by
urgency: *Blocked* (a permission prompt or question, oldest first, with how
long it has waited), *Working* (what it is doing now, in plain words; a session
silent for 10 minutes turns red as possibly stuck), *Ready for you* (finished,
waiting for your next message), then *Idle* and *Closed today* folded away. Tap
a row for a plain-language timeline. It is built for a phone first and reads
the same event stream; the original page stays at `/classic`.

Each session appears under a project named after its folder (a Claude Code
worktree reports its repository's folder). Events also carry `host` and
`session` fields for later filtering; the dashboard ignores fields it does not
know. Tests: `python -m unittest tests.test_claude_code_hook`.

---

## Viewing from your phone

Keep `bind` at `127.0.0.1` and let [Tailscale](https://tailscale.com) carry
the dashboard to your phone, inside your tailnet only:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:7000
```

Then list the devices allowed to look, by tailnet address (IPv4 and IPv6):

```json
{ "remote_clients": ["100.64.0.10", "fd7a:115c:a1e0::10"] }
```

**How the gate decides.** Every HTTP and websocket request passes it:
- **Local requests:** from loopback, with no proxy headers, and a `Host` of `127.0.0.1`, `localhost` or `[::1]`. These are allowed. The Host check refuses DNS-rebinding pages and forwarders that add no headers.
- **Proxied requests:** from loopback, carrying `X-Forwarded-For` or `Tailscale-User-*`. These are allowed only when there is exactly one `X-Forwarded-For` header and its value is on the list. Tailscale serve replaces any client-sent values of those headers with the real ones.
- **Everything else** gets a 403 and one log line.

**Rules that keep it safe:**
- Start the server with `python server.py`. It turns off uvicorn's proxy-header rewriting, which would otherwise hide the real peer from the gate.
- Never use `tailscale funnel`, which publishes to the internet.
- Prefer `--https` with a neutral machine name. HTTPS certificates put the machine's name in public certificate-transparency logs.

On iPhone, open the `https://<machine>.<tailnet>.ts.net` address in Safari, then choose Share and **Add to Home Screen**.

---

## Optional Telegram relay

Disabled by default. When configured, a background thread polls a Telegram bot
for `/dashboard` or `/status` commands and replies with your dashboard URL.
It has **no other send authority** and cannot act on any agent.

```json
{
  "telegram": {
    "bot_token": "YOUR_BOT_TOKEN",
    "chat_id":   "YOUR_CHAT_ID",
    "dashboard_url": "http://your-tailscale-host:7000"
  }
}
```

Requires: `pip install httpx`

---

## Project layout

```
agent-command-center/
├── server.py              # FastAPI server (SSE + static serving)
├── telegram_relay.py      # Optional Telegram relay module
├── requirements.txt
├── config.example.json    # Template — copy to config.json
├── CONTRACT.md            # Full event schema specification
├── static/
│   ├── index.html         # Single-page dashboard (no JS framework)
│   └── emblem.svg         # Default geometric emblem (swappable via config)
├── examples/
│   ├── emit_demo.py       # Demo emitter — run to see the dashboard populate
│   └── claude_code_hook.py # Claude Code hook: every session becomes an agent
└── tests/                 # python -m unittest discover tests
```

---

## Credits

Designed &amp; built by **Clayton Cuteri**. © 2026 Clayton Cuteri.

## License

MIT — see [LICENSE](LICENSE). Copyright © 2026 Clayton Cuteri.
