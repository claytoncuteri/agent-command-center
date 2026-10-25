"""
Tests for static/sessions.js, the session model behind the session-first view.

The model is plain JavaScript; these tests run it under Node and check the
result. They are skipped when `node` is not installed.

Run from the repo root:  python -m unittest tests.test_sessions_js
"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODEL = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

NOW = "2026-10-24T12:00:00.000Z"

HARNESS = r"""
const m = require(process.argv[1]);
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const now = Date.parse(input.now);
const out = m.deriveSessions(input.events, now, input.bootedAt ? { bootedAt: Date.parse(input.bootedAt) } : undefined);
out.sessions.forEach(s => { s.line = m.describe(s, now); s.timeline = s.events.map(m.eventLine); delete s.events; });
process.stdout.write(JSON.stringify(out));
"""


def ev(minutes_ago, status, action, session="s1", agent="Demo", **extra):
    """An event `minutes_ago` before NOW."""
    base = 1792843200000  # NOW in ms
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp((base - minutes_ago * 60000) / 1000, tz=timezone.utc)
    e = {
        "schema": "acc/1", "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "agent": agent, "action": action, "status": status,
        "source": "claude-code", "host": "mac", "session": session,
    }
    e.update(extra)
    return e


@unittest.skipIf(NODE is None, "node is not installed")
class TestSessionModel(unittest.TestCase):
    def derive(self, events, booted_at=None):
        proc = subprocess.run(
            [NODE, "-e", HARNESS, str(MODEL)],
            input=json.dumps({"now": NOW, "events": events, "bootedAt": booted_at}),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def one(self, events):
        out = self.derive(events)
        self.assertEqual(len(out["sessions"]), 1)
        return out["sessions"][0]

    def test_now_matches_constant(self):
        from datetime import datetime
        self.assertEqual(
            int(datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp() * 1000), 1792843200000
        )

    def test_one_row_per_session_not_per_folder(self):
        out = self.derive([
            ev(30, "idle", "Session opened", session="a"),
            ev(29, "idle", "Session opened", session="b"),
        ])
        self.assertEqual(len(out["sessions"]), 2)
        self.assertEqual({s["project"] for s in out["sessions"]}, {"Demo"})

    def test_needs_you_with_plain_ask_and_wait_time(self):
        s = self.one([
            ev(20, "started", "Working", doing="Thinking"),
            ev(14, "awaiting_approval", "Needs permission", approval_id="p1", detail="permission: Bash"),
        ])
        self.assertEqual(s["state"], "need")
        self.assertEqual(s["ask"], {"kind": "Permission", "text": "run a shell command (Bash)"})
        self.assertEqual(s["line"], "Permission: run a shell command (Bash)")

    def test_answered_permission_goes_back_to_working_not_stale_ask(self):
        s = self.one([
            ev(5, "started", "Working", doing="Thinking"),
            ev(4, "awaiting_approval", "Needs permission", approval_id="p1", detail="permission: Bash"),
            ev(3, "approved", "Permission granted", approval_id="p1"),
        ])
        self.assertEqual(s["state"], "work")
        self.assertIsNone(s["ask"])

    def test_app_tool_permissions_read_plainly(self):
        out = self.derive([
            ev(3, "awaiting_approval", "Needs permission", approval_id="a", session="a",
               detail="permission: mcp__ccd_session__start_session"),
            ev(2, "awaiting_approval", "Needs permission", approval_id="b", session="b",
               detail="permission: mcp__ccd_view__show_pane"),
            ev(1, "awaiting_approval", "Needs permission", approval_id="c", session="c",
               detail="permission: mcp__claude-in-chrome__navigate"),
        ])
        self.assertEqual([s["line"] for s in out["sessions"]], [
            "Permission: start a new session",
            "Permission: use the app (show pane)",
            "Permission: use the browser",
        ])

    def test_question(self):
        s = self.one([ev(2, "awaiting_approval", "Question for you", approval_id="q", detail="question")])
        self.assertEqual(s["line"], "Question: Claude is asking you to choose")

    def test_working_shows_doing_and_turn_time(self):
        s = self.one([
            ev(6, "started", "Working", doing="Thinking"),
            ev(5, "started", "Working", doing="Searching the code"),
            ev(1, "started", "Working", doing="Editing files"),
        ])
        self.assertEqual(s["state"], "work")
        self.assertEqual(s["line"], "Editing files · turn running 6m")

    def test_quiet_after_ten_silent_minutes(self):
        s = self.one([
            ev(30, "started", "Working", doing="Thinking"),
            ev(24, "started", "Working", doing="Searching the code"),
        ])
        self.assertEqual(s["state"], "quiet")
        self.assertIn("silent 24m", s["line"])

    def test_your_turn_with_turn_duration(self):
        s = self.one([
            ev(12, "started", "Working", doing="Thinking"),
            ev(8, "finished", "Your turn"),
        ])
        self.assertEqual(s["state"], "turn")
        self.assertEqual(s["line"], "Finished a turn after 4m")

    def test_fresh_session_waits_for_first_message(self):
        s = self.one([ev(3, "idle", "Session opened")])
        self.assertEqual(s["state"], "turn")
        self.assertEqual(s["line"], "Opened, waiting for your first message")

    def test_idle_after_six_hours(self):
        s = self.one([ev(7 * 60, "started", "Working"), ev(7 * 60 - 2, "finished", "Your turn")])
        self.assertEqual(s["state"], "idle")
        self.assertEqual(s["line"], "Last: your turn")

    def test_closed_today_kept_older_dropped(self):
        out = self.derive([
            ev(60, "idle", "Session closed", session="today"),
            ev(30 * 60, "idle", "Session closed", session="old"),
        ])
        self.assertEqual([s["session"] for s in out["sessions"]], ["today"])
        self.assertEqual(out["counts"]["closed"], 1)

    def test_error_counts_as_needing_you(self):
        out = self.derive([ev(2, "failed", "Nightly sync", detail="disk full", agent="Ops", session=None)])
        s = out["sessions"][0]
        self.assertEqual(s["state"], "error")
        self.assertEqual(out["counts"]["need"], 1)
        self.assertEqual(s["line"], "Error: disk full")

    def test_order_needs_oldest_first_then_working_then_turn(self):
        out = self.derive([
            ev(1, "finished", "Your turn", session="turn"),
            ev(2, "started", "Working", doing="Editing files", session="work"),
            ev(3, "awaiting_approval", "Needs permission", approval_id="n2", detail="permission: Edit", session="need-new"),
            ev(9, "awaiting_approval", "Needs permission", approval_id="n1", detail="permission: Bash", session="need-old"),
        ])
        self.assertEqual([s["session"] for s in out["sessions"]], ["need-old", "need-new", "work", "turn"])
        self.assertEqual(out["counts"], {"need": 2, "work": 1, "turn": 1, "idle": 0, "closed": 0})

    def test_title_and_projects(self):
        out = self.derive([
            ev(5, "idle", "Session opened", title="Website: Build the /contact page", agent="Website"),
            ev(4, "awaiting_approval", "Needs permission", approval_id="x", detail="permission: Bash", agent="Website"),
            ev(3, "idle", "Session opened", session="s2", agent="Other"),
        ])
        titled = [s for s in out["sessions"] if s["project"] == "Website"][0]
        self.assertEqual(titled["title"], "Website: Build the /contact page")
        self.assertEqual(out["projects"][0], {"name": "Website", "total": 1, "need": 1})

    def test_generic_agent_stream_without_sessions(self):
        out = self.derive([
            {"ts": "2026-10-24T11:58:00Z", "agent": "SCOUT", "action": "Inbox scan", "status": "started"},
            {"ts": "2026-10-24T11:59:00Z", "agent": "KEEPER", "action": "Email stakeholders",
             "status": "awaiting_approval", "approval_id": "a1", "detail": "3 recipients"},
        ])
        by = {s["project"]: s for s in out["sessions"]}
        self.assertEqual(by["SCOUT"]["state"], "work")
        self.assertEqual(by["KEEPER"]["line"], "Approval: Email stakeholders · 3 recipients")

    def test_activity_bar_marks_recent_slots(self):
        s = self.one([
            ev(50, "started", "Working", doing="Thinking"),
            ev(2, "awaiting_approval", "Needs permission", approval_id="b", detail="permission: Bash"),
        ])
        self.assertEqual(len(s["bars"]), 12)
        self.assertEqual(s["bars"][-1], "a")
        self.assertIn("w", s["bars"][:3])

    def test_timeline_is_plain_language_newest_first(self):
        s = self.one([
            ev(10, "idle", "Session opened"),
            ev(9, "started", "Working", doing="Thinking"),
            ev(8, "started", "Working", doing="Editing files"),
            ev(7, "awaiting_approval", "Needs permission", approval_id="t", detail="permission: Bash"),
        ])
        self.assertEqual(s["timeline"], [
            "Asked permission to run a shell command (Bash)",
            "Editing files",
            "You sent a message; working",
            "Session opened",
        ])

    def test_restart_cuts_off_working_and_blocked_sessions(self):
        booted = "2026-10-24T11:30:00.000Z"  # 30 minutes before NOW
        out = self.derive([
            ev(50, "started", "Working", doing="Editing files", session="work"),
            ev(45, "awaiting_approval", "Needs permission", approval_id="p", detail="permission: Bash", session="need"),
            ev(40, "finished", "Your turn", session="turn"),
            ev(5, "started", "Working", doing="Running a command", session="after"),
        ], booted_at=booted)
        by = {s["session"]: s for s in out["sessions"]}
        self.assertEqual(by["work"]["state"], "idle")
        self.assertTrue(by["work"]["interrupted"])
        self.assertEqual(by["work"]["line"], "Was working when the computer restarted · open it to carry on")
        self.assertEqual(by["need"]["state"], "idle")
        self.assertIsNone(by["need"]["ask"])
        self.assertTrue(by["need"]["line"].startswith("Was waiting on you"))
        self.assertEqual(by["turn"]["state"], "turn", "a finished turn is still ready after a restart")
        self.assertEqual(by["after"]["state"], "work", "activity after the restart is live")
        self.assertEqual(out["counts"]["need"], 0)

    def test_orphan_ask_cleared_at_the_next_turn(self):
        s = self.one([
            ev(40, "awaiting_approval", "Question for you", approval_id="orphan", detail="question"),
            ev(40, "awaiting_approval", "Question for you", approval_id="kept", detail="question"),
            ev(38, "approved", "Question answered", approval_id="kept"),
            ev(30, "finished", "Your turn"),
            ev(5, "started", "Working", doing="Thinking"),
        ])
        self.assertEqual(s["state"], "work", "the orphaned ask must not keep the session Blocked")

    def test_other_stacks_keep_asks_across_tasks(self):
        out = self.derive([
            {"ts": "2026-10-24T11:50:00Z", "agent": "KEEPER", "action": "Email", "status": "awaiting_approval", "approval_id": "a"},
            {"ts": "2026-10-24T11:55:00Z", "agent": "KEEPER", "action": "Inbox scan", "status": "finished"},
        ])
        self.assertEqual(out["sessions"][0]["state"], "need")

    def test_step_progress_on_working_and_ready_rows(self):
        s = self.one([
            ev(6, "started", "Working", doing="Thinking"),
            ev(5, "started", "Working", doing="Updating its task list", steps_done=2, steps_total=6, steps_current=3),
            ev(1, "started", "Working", doing="Editing files"),
        ])
        self.assertEqual(s["steps"], {"done": 2, "total": 6, "current": 3})
        self.assertEqual(s["line"], "Step 3 of 6 · Editing files · turn running 6m")
        s = self.one([
            ev(9, "started", "Working", doing="Thinking"),
            ev(8, "started", "Working", steps_done=4, steps_total=6, steps_current=5),
            ev(5, "finished", "Your turn"),
        ])
        self.assertEqual(s["line"], "Finished a turn after 4m · 4 of 6 steps done")

    def test_all_steps_done(self):
        s = self.one([
            ev(3, "started", "Working", doing="Thinking"),
            ev(2, "started", "Working", doing="Updating its task list", steps_done=5, steps_total=5),
        ])
        self.assertTrue(s["line"].startswith("All 5 steps done · "))
        s = self.one([ev(9, "started", "Working", steps_done=5, steps_total=5), ev(5, "finished", "Your turn")])
        self.assertNotIn("steps", s["line"])

    def test_new_session_drops_old_steps_and_bad_counts_are_clamped(self):
        s = self.one([ev(9, "started", "Working", steps_done=1, steps_total=3), ev(5, "idle", "Session opened")])
        self.assertIsNone(s["steps"])
        s = self.one([ev(2, "started", "Working", steps_done=99, steps_total=4)])
        self.assertEqual(s["steps"]["done"], 4)

    def test_no_boot_time_keeps_old_behaviour(self):
        s = self.one([ev(50, "started", "Working", doing="Editing files")])
        self.assertEqual(s["state"], "quiet")

    def test_bad_events_are_ignored(self):
        out = self.derive([{"agent": "X"}, {"ts": "nope", "agent": "Y"}, None, {"ts": NOW}])
        self.assertEqual(out["sessions"], [])


if __name__ == "__main__":
    unittest.main()
