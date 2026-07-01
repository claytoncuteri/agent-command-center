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

Point `logo` at a local file path or a `data:` URI.
The file is read by your browser, not committed to the repo.

```json
{ "logo": "/Users/yourname/.acc/logo.png" }
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
└── examples/
    └── emit_demo.py       # Demo emitter — run to see the dashboard populate
```

---

## Credits

Designed &amp; built by **Clayton Cuteri**. © 2026 Clayton Cuteri.

## License

MIT — see [LICENSE](LICENSE). Copyright © 2026 Clayton Cuteri.
