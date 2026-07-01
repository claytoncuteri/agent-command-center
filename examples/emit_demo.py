#!/usr/bin/env python3
"""
emit_demo.py — populate a local events.jsonl with sample events.

Run this in one terminal, then run `python server.py` in another.
Open http://127.0.0.1:7000 and watch the dashboard populate in real time.

Usage:
    python examples/emit_demo.py                   # writes to ./events.jsonl
    python examples/emit_demo.py path/to/file.jsonl
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("events.jsonl")


def load_content_mode() -> str:
    """Best-effort read of content_mode from config.json (default 'inline')."""
    try:
        with open("config.json", encoding="utf-8") as f:
            mode = json.load(f).get("content_mode", "inline")
        return mode if mode in ("inline", "reference") else "inline"
    except Exception:
        return "inline"


# A real, generic, multi-paragraph draft the reviewer can read and edit.
DIGEST_BLOCKS = [
    {"label": "Subject", "text": "Weekly Digest — Week 27"},
    {
        "label": "Intro",
        "text": (
            "Team,\n\n"
            "Here is the rundown for the week. It was a busy one: we shipped two "
            "features, closed out a backlog of small fixes, and started planning "
            "next month's work."
        ),
    },
    {
        "label": "Highlights",
        "text": (
            "- Shipped the new onboarding flow to all users.\n"
            "- Cut median API latency by roughly a third.\n"
            "- Resolved 18 support threads and archived the stale ones."
        ),
    },
    {
        "label": "Next steps",
        "text": (
            "Next week we focus on the reporting dashboard and a first pass at "
            "the export pipeline. Please review the draft spec before Tuesday and "
            "leave comments inline."
        ),
    },
]


def digest_content_kwargs(mode: str) -> dict:
    """Return the emit kwargs carrying the digest draft for the given content mode."""
    if mode == "reference":
        drafts_dir = Path("drafts")
        drafts_dir.mkdir(exist_ok=True)
        draft_path = drafts_dir / "weekly_digest.md"
        text = "\n\n".join(f"## {b['label']}\n{b['text']}" for b in DIGEST_BLOCKS)
        draft_path.write_text(text, encoding="utf-8")
        print(f"  (reference mode) wrote draft → {draft_path}")
        return {"content_ref": {"id": "digest-w27", "path": "drafts/weekly_digest.md"}}
    # inline (default): the full draft travels in the event
    return {"content": DIGEST_BLOCKS}


def emit(agent: str, action: str, status: str, **kwargs) -> None:
    event = {
        "schema": "acc/1",
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "action": action,
        "status": status,
        **kwargs,
    }
    with OUTPUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    tok = f" [{event.get('tokens', '')} tok]" if event.get("tokens") else ""
    print(f"[{event['ts'][11:19]}] {agent:10s} {status:22s} {action[:50]}{tok}")


def main() -> None:
    content_mode = load_content_mode()
    print(f"Writing demo events to: {OUTPUT.resolve()}")
    print(f"Content mode: {content_mode}")
    print("Open the dashboard, then watch it update.\n")

    # ── Phase 1: agents boot up ────────────────────────────────────────────
    emit("SCOUT",  "Morning inbox scan",           "started",  category="inbox",   level="info")
    time.sleep(0.4)
    emit("KEEPER", "Reconcile overnight records",   "started",  category="finance", level="info")
    time.sleep(0.4)
    emit("HERALD", "System health check",          "started",  category="ops",     level="info")
    time.sleep(0.4)
    emit("ANCHOR", "Draft weekly digest",          "started",  category="content", level="info")
    time.sleep(2)

    # ── Phase 2: some finish quickly ──────────────────────────────────────
    emit("HERALD", "System health check",          "finished",
         result="All 4 services nominal", tokens=980, level="info")
    time.sleep(1.5)

    emit("KEEPER", "Reconcile overnight records",   "finished",
         result="52 line items reconciled", tokens=3400, level="info")
    time.sleep(1)

    # ── Phase 3: approval flow ────────────────────────────────────────────
    appr_id = "appr-" + str(uuid.uuid4())[:8]
    emit("SCOUT",  "Email stakeholder summary",    "awaiting_approval",
         approval_id=appr_id,
         detail="Ready to send to 3 recipients",
         tokens=2100, level="flash")
    time.sleep(3)

    # ANCHOR progresses
    emit("ANCHOR", "Draft weekly digest",          "info",
         detail="Outline complete, writing sections", level="info")
    time.sleep(2)

    # Human approves (in your real system this would come from the agent)
    emit("SCOUT",  "Email stakeholder summary",    "approved",
         approval_id=appr_id, level="info")
    time.sleep(0.8)

    emit("SCOUT",  "Email stakeholder summary",    "finished",
         approval_id=appr_id,
         result="Delivered to 3 recipients", tokens=820, level="info")
    time.sleep(1.5)

    # ── Phase 4: more agents join ─────────────────────────────────────────
    emit("RELAY",  "Archive completed threads",    "started",  category="inbox",   level="info")
    time.sleep(1)

    emit("ANCHOR", "Draft weekly digest",          "finished",
         result="Draft complete \u2014 6 items, 840 words", tokens=5200, level="info")
    time.sleep(1)

    emit("RELAY",  "Archive completed threads",    "finished",
         result="18 threads archived", tokens=1100, level="info")
    time.sleep(1)

    # ── Phase 5: a second approval — carries the full reviewable draft ────
    appr2 = "appr-" + str(uuid.uuid4())[:8]
    emit("ANCHOR", "Publish weekly digest",        "awaiting_approval",
         approval_id=appr2,
         category="content",
         detail="Targets: internal newsletter + 2 channels",
         tokens=420, level="flash",
         **digest_content_kwargs(content_mode))
    time.sleep(3)

    # ── Phase 6: idle / info events ───────────────────────────────────────
    emit("SCOUT",  "Idle",                         "idle",     level="info")
    time.sleep(0.5)
    emit("HERALD", "Uptime monitor ping",          "info",
         detail="All services nominal at 15-min check", tokens=120, level="info")
    time.sleep(0.5)
    emit("KEEPER", "Idle",                         "idle",     level="info")
    time.sleep(0.5)

    print(f"\nDone. {OUTPUT} now has events.")
    print(f"Leave the dashboard open \u2014 ANCHOR still has an open approval with a")
    print(f"full multi-paragraph draft. Open it, Revise a paragraph, optionally add a")
    print(f"reason, and Submit (browser/both mode) to write a corrections.jsonl entry.")


if __name__ == "__main__":
    main()
