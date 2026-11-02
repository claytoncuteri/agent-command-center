"""
Tests for static/focus.js, the model behind the focus view (one session in
depth, following the one you have in front).

The model is plain JavaScript; these tests run it under Node and check the
result. They are skipped when `node` is not installed.

Run from the repo root:  python -m unittest tests.test_focus_js
"""
import json
import pathlib
import shutil
import subprocess
import unittest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "static" / "sessions.js"
FOCUS = ROOT / "static" / "focus.js"
NODE = shutil.which("node")

NOW = "2026-10-24T12:00:00.000Z"
NOW_MS = 1792843200000
DAY_START = "2026-10-24T04:00:00.000Z"  # local midnight, as the page would pass it

HARNESS = r"""
const S = require(process.argv[1]);
const F = require(process.argv[2]);
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const now = Date.parse(input.now);
const model = S.deriveSessions(input.events, now, {});
const key = F.pickFocus(model.sessions, input.links || {}, input.pinned || null);
const s = model.sessions.filter(x => x.key === key)[0] || null;
const turn = s ? F.currentTurn(s, now) : null;
const out = {
  focus: s ? s.session : null,
  turn: turn && { start: turn.start, end: turn.end, live: turn.live,
                  spans: turn.spans.map(sp => [sp.label, Math.round((sp.to - sp.from) / 60000)]) },
  mix: F.timeMix(turn).map(m => [m.label, Math.round(m.ms / 60000), Math.round(m.share * 100)]),
  day: s ? F.dayStats(s, now, Date.parse(input.dayStart)) : null,
  timeline: s ? F.foldTimeline(s, input.limit || 0).map(x => [x.line, x.count, Math.round((x.at - x.from) / 60000)]) : [],
  others: F.others(model.sessions, key).map(x => x.session),
  blocked: F.blockedElsewhere(model.sessions, key).map(x => x.session),
  clocks: (input.clocks || []).map(F.clock),
};
process.stdout.write(JSON.stringify(out));
"""


def ev(minutes_ago, status, action, session="s1", agent="Demo", **extra):
    """An event `minutes_ago` before NOW."""
    ts = datetime.fromtimestamp((NOW_MS - minutes_ago * 60000) / 1000, tz=timezone.utc)
    e = {
        "schema": "acc/1", "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "agent": agent, "action": action, "status": status,
        "source": "claude-code", "host": "mac", "session": session,
    }
    e.update(extra)
    return e


def prompt(minutes_ago, session="s1"):
    return ev(minutes_ago, "started", "Working", session=session, doing="Thinking")


def doing(minutes_ago, label, session="s1"):
    return ev(minutes_ago, "started", "Working", session=session, doing=label)


def finished(minutes_ago, session="s1"):
    return ev(minutes_ago, "finished", "Your turn", session=session)


@unittest.skipIf(NODE is None, "node is not installed")
class TestFocusModel(unittest.TestCase):
    def run_model(self, events, links=None, pinned=None, limit=0, clocks=None):
        proc = subprocess.run(
            [NODE, "-e", HARNESS, str(SESSIONS), str(FOCUS)],
            input=json.dumps({"now": NOW, "dayStart": DAY_START, "events": events, "links": links,
                              "pinned": pinned, "limit": limit, "clocks": clocks}),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    # ── Which session ────────────────────────────────────────────────────────

    def test_follows_the_session_the_app_showed_last(self):
        out = self.run_model(
            [prompt(30, "a"), prompt(20, "b")],
            links={"a": {"focused_at": NOW_MS - 60000}, "b": {"focused_at": NOW_MS - 600000}},
        )
        self.assertEqual(out["focus"], "a", "focused a minute ago beats a message 20 minutes ago")

    def test_a_newer_message_beats_an_older_focus(self):
        out = self.run_model(
            [prompt(30, "a"), prompt(2, "b")],
            links={"a": {"focused_at": NOW_MS - 300000}},
        )
        self.assertEqual(out["focus"], "b")

    def test_pinned_wins_while_listed(self):
        out = self.run_model([prompt(30, "a"), prompt(2, "b")], pinned="mac|a")
        self.assertEqual(out["focus"], "a")
        out = self.run_model([prompt(30, "a"), prompt(2, "b")], pinned="mac|gone")
        self.assertEqual(out["focus"], "b", "a pin to a session that is not listed is ignored")

    def test_without_signals_the_most_urgent_session_wins(self):
        out = self.run_model([
            ev(40, "idle", "Session opened", session="quiet"),
            ev(30, "idle", "Session opened", session="asker"),
            ev(5, "awaiting_approval", "Needs permission", session="asker", approval_id="p", detail="permission: Bash"),
        ])
        self.assertEqual(out["focus"], "asker")

    def test_closed_sessions_are_never_chosen(self):
        out = self.run_model(
            [prompt(30, "a"), prompt(3, "b"), ev(1, "idle", "Session closed", session="b")],
            links={"b": {"focused_at": NOW_MS}},
        )
        self.assertEqual(out["focus"], "a")
        self.assertEqual(out["others"], [])

    # ── The turn ─────────────────────────────────────────────────────────────

    def test_live_turn_spans_and_mix(self):
        out = self.run_model([
            prompt(10), doing(9, "Running a command"), doing(5, "Editing files"), doing(4, "Running a command"),
        ])
        self.assertTrue(out["turn"]["live"])
        self.assertIsNone(out["turn"]["end"])
        self.assertEqual(out["turn"]["spans"], [
            ["Thinking", 1], ["Running a command", 4], ["Editing files", 1], ["Running a command", 4]])
        self.assertEqual(out["mix"], [["Running a command", 8, 80], ["Thinking", 1, 10], ["Editing files", 1, 10]])

    def test_finished_turn_ends_at_the_turn_end_and_counts_waiting(self):
        out = self.run_model([
            prompt(30), doing(29, "Running a command"),
            ev(25, "awaiting_approval", "Needs permission", approval_id="p", detail="permission: Bash"),
            ev(20, "approved", "Permission granted", approval_id="p"),
            finished(10), doing(1, "Reading files", session="other"),
        ])
        self.assertEqual(out["focus"], "s1", "the finished turn is the one the app last touched here")
        self.assertFalse(out["turn"]["live"])
        self.assertEqual(out["turn"]["spans"], [
            ["Thinking", 1], ["Running a command", 4], ["Waiting for you", 5], ["Running a command", 10]])

    def test_step_progress_events_do_not_split_spans(self):
        out = self.run_model([
            prompt(6), doing(5, "Editing files"),
            ev(3, "started", "Working", doing="Editing files", steps_done=1, steps_total=3, steps_current=2),
        ])
        self.assertEqual(out["turn"]["spans"], [["Thinking", 1], ["Editing files", 5]])

    def test_resume_mid_turn_does_not_end_it(self):
        # A message resumes a session (the hook's "resumed" follows the prompt),
        # and the context is compacted later in the same turn.
        out = self.run_model([
            prompt(20), ev(20, "idle", "Session resumed"), doing(19, "Running a command"),
            ev(10, "idle", "Session resumed"), doing(9, "Editing files"), finished(2),
        ])
        self.assertFalse(out["turn"]["live"])
        self.assertEqual(out["turn"]["spans"], [["Thinking", 1], ["Running a command", 10], ["Editing files", 7]])
        self.assertEqual(out["day"]["turns"], 1)
        self.assertEqual(round(out["day"]["workedMs"] / 60000), 18)

    def test_no_turn_yet(self):
        out = self.run_model([ev(5, "idle", "Session opened")])
        self.assertIsNone(out["turn"])
        self.assertEqual(out["mix"], [])

    # ── The day ──────────────────────────────────────────────────────────────

    def test_day_stats_count_turns_work_and_waiting_since_day_start(self):
        out = self.run_model([
            prompt(600), finished(590),                 # yesterday: not counted
            prompt(100), finished(70),                  # 30 minutes of work
            prompt(50),
            ev(45, "awaiting_approval", "Needs permission", approval_id="p", detail="permission: Bash"),
            ev(41, "approved", "Permission granted", approval_id="p"),
            finished(40),                               # 10 minutes of work, 4 waiting
            prompt(5),                                  # live: 5 minutes so far
        ])
        d = out["day"]
        self.assertEqual(d["turns"], 3)
        self.assertEqual(round(d["workedMs"] / 60000), 45)
        self.assertEqual(round(d["waitedMs"] / 60000), 4)

    def test_turn_that_crosses_midnight_counts_only_today(self):
        # Day start is 8h before NOW; the turn began 9h before and ended 7h before.
        out = self.run_model([prompt(540), finished(420)])
        self.assertEqual(out["day"]["turns"], 0)
        self.assertEqual(round(out["day"]["workedMs"] / 60000), 60)

    # ── The timeline ─────────────────────────────────────────────────────────

    def test_repeated_lines_fold_with_a_count_and_span(self):
        out = self.run_model([
            prompt(10), doing(9, "Running a command"), doing(7, "Editing files"),
            doing(6, "Running a command"), doing(4, "Running a command"), doing(2, "Running a command"),
        ])
        self.assertEqual(out["timeline"], [
            ["Running a command", 3, 4], ["Editing files", 1, 0], ["Running a command", 1, 0],
            ["You sent a message; working", 1, 0],
        ])

    def test_timeline_limit_counts_folded_entries(self):
        out = self.run_model([prompt(10), doing(9, "A"), doing(8, "B"), doing(7, "C"), doing(6, "C")], limit=2)
        self.assertEqual([x[0] for x in out["timeline"]], ["C", "B"])

    # ── Others and alerts ────────────────────────────────────────────────────

    def test_others_and_blocked_elsewhere(self):
        out = self.run_model([
            prompt(1, "me"), prompt(20, "b"), prompt(30, "c"),
            ev(3, "awaiting_approval", "Needs permission", session="c", approval_id="p", detail="permission: Bash"),
            ev(50, "idle", "Session opened", session="d"), ev(49, "idle", "Session closed", session="d"),
        ])
        self.assertEqual(out["focus"], "me")
        self.assertEqual(out["others"], ["c", "b"], "most urgent first, the focused and closed ones left out")
        self.assertEqual(out["blocked"], ["c"])

    def test_clock_format(self):
        out = self.run_model([prompt(1)], clocks=[0, 4000, 65000, 3600000 + 3 * 60000 + 9000, -5])
        self.assertEqual(out["clocks"], ["0m 00s", "0m 04s", "1m 05s", "1h 03m 09s", ""])


if __name__ == "__main__":
    unittest.main()
