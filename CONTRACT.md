# Agent Command Center — Event Contract

Version: `acc/1`

Any agent system that emits newline-delimited JSON (NDJSON) conforming to this
schema can be visualised by the Agent Command Center dashboard without any
code changes.

---

## File format

One JSON object per line in an **append-only** `.jsonl` file.
Blank lines and lines that fail JSON parsing are silently skipped.

```
{"schema":"acc/1","id":"...","ts":"...","agent":"SCOUT", ...}
{"schema":"acc/1","id":"...","ts":"...","agent":"KEEPER", ...}
```

---

## Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema` | string | recommended | Must be `"acc/1"` for this version. Future versions may differ. |
| `id` | string | recommended | Unique event identifier (UUID recommended). |
| `ts` | string | **yes** | ISO 8601 timestamp, e.g. `2025-06-30T09:14:22.341Z`. UTC preferred. |
| `agent` | string | **yes** | Agent name. The UI renders whatever names appear; never hardcoded. |
| `action` | string | **yes** | Short description of the task, e.g. `"Morning inbox scan"`. |
| `status` | string | **yes** | See status vocabulary below. |
| `category` | string | no | Arbitrary tag grouping tasks, e.g. `"inbox"`, `"finance"`. |
| `level` | string | no | `"info"` (default) or `"flash"` for high-priority items. |
| `tokens` | integer | no | Token count consumed by this event. Used for usage meters. |
| `approval_id` | string | no | Stable ID linking approval lifecycle events together. |
| `result` | string | no | Short summary of the outcome once finished. |
| `detail` | string | no | Free-form detail text shown in the agent card or approval panel. |
| `content` | array | no | Reviewable draft as labeled blocks (inline content mode). See below. |
| `content_ref` | object/string | no | Pointer to a draft on disk (reference content mode). See below. |

Unknown fields are ignored. Missing optional fields are gracefully omitted
from the UI without error.

### Reviewable draft content

An `awaiting_approval` event may carry the actual draft the reviewer should see —
in one of two shapes (the dashboard renders whichever is present):

- **Inline** — `content` is an array of blocks, each `{ "label", "text", "path"? }`.
  The full draft travels in the event stream. Simple default.

  ```json
  {"status":"awaiting_approval","approval_id":"appr-1","content":[
    {"label":"Subject","text":"Weekly Digest — Week 27"},
    {"label":"Body","text":"Team,\n\nHere is the rundown…"}
  ]}
  ```

- **Reference** — `content_ref` is `{ "id"?, "path" }` (or a plain path string).
  Only a pointer travels in the stream; the dashboard reads the draft from disk via
  `GET /api/content?ref=<path>` (sandboxed to the configured `content_dir`). Keeps
  the event log lean and drafts out of telemetry.

  ```json
  {"status":"awaiting_approval","approval_id":"appr-1",
   "content_ref":{"id":"digest-w27","path":"drafts/weekly_digest.md"}}
  ```

When a reviewer edits and submits the draft (browser action mode), the dashboard
records the original + a structured diff (+ optional reason) to a **separate**
`corrections.jsonl` — never in this event stream. See the README for details.

---

## Status vocabulary

| Status | Meaning | Visual |
|---|---|---|
| `started` | Agent has begun a task | Active (green border, progress sweep) |
| `finished` | Task completed successfully | Idle |
| `failed` | Task ended in an error | Error state (red indicator) |
| `awaiting_approval` | Agent is blocked waiting for human sign-off | Standby (gold, pulsing approval panel) |
| `approved` | A pending approval was granted | Resolves the matching `approval_id` |
| `revised` | Approval content was revised before submission | Standby |
| `declined` | Approval was denied | Resolves the matching `approval_id` |
| `idle` | Agent is standing by with no active task | Idle |
| `info` | Informational log entry, not a task state change | Idle / logged only |

---

## Approval lifecycle

Use `approval_id` to link events in an approval flow:

```jsonl
{"ts":"...","agent":"SCOUT","action":"Email stakeholders","status":"awaiting_approval","approval_id":"appr-001","detail":"3 recipients"}
{"ts":"...","agent":"SCOUT","action":"Email stakeholders","status":"approved","approval_id":"appr-001"}
{"ts":"...","agent":"SCOUT","action":"Email stakeholders","status":"finished","approval_id":"appr-001","result":"Sent to 3 recipients","tokens":820}
```

An `awaiting_approval` event with a given `approval_id` is considered **open**
(shown in the approvals panel) until an `approved` or `declined` event with
the same `approval_id` appears in the stream. The dashboard is read-only; the
approval action itself happens in your agent system.

---

## How the UI consumes the stream

| UI section | Derived from |
|---|---|
| Agent status cards | Latest event per `agent` value (highest `ts`) |
| Activity log | All events, sorted by `ts` descending, capped at ~30 |
| Approvals panel | `awaiting_approval` events whose `approval_id` has no later `approved`/`declined` match |
| Usage meters | Sum of `tokens` per agent from the current session (reloads with page) |
| Stats bar | Counts aggregated across all loaded events |

---

## Minimal emitter example

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

# Usage
emit("events.jsonl", agent="SCOUT", action="Morning scan", status="started")
emit("events.jsonl", agent="SCOUT", action="Morning scan", status="finished",
     result="38 items processed", tokens=2100)
```

A runnable demo that populates a local `events.jsonl` is in `examples/emit_demo.py`.
