"""
Tests for static/whitehouse.js, the White House view's building model.

Run from the repo root (needs node):  python -m unittest tests.test_whitehouse_js
The model is fed the output of sessions.js deriveSessions, as the page does.
"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "static" / "sessions.js"
MODEL = ROOT / "static" / "whitehouse.js"
NODE = shutil.which("node")
NOW = "2026-10-24T12:00:00.000Z"

HARNESS = r"""
const S = require(process.argv[1]);
const W = require(process.argv[2]);
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const now = Date.parse(input.now);
const opts = {};
if (input.summaries) opts.summaries = input.summaries;
const sessions = S.deriveSessions(input.events, now, opts).sessions;
const deps = W.buildDepartments(sessions, now);
const lists = W.deskLists(deps, now);
const strip = x => ({ title: x.title, session: x.session, standing: x.standing, need: x.need, project: x.s.project });
process.stdout.write(JSON.stringify({
  departments: deps.map(d => ({ name: d.name, decisions: d.decisions, updates: d.updates, working: d.working,
    idle: d.idle, inOval: d.inOval, drawn: d.drawn, desks: d.desks, staff: d.staff.map(strip) })),
  lists: { decisions: lists.decisions.map(strip), updates: lists.updates.map(strip), finished: lists.finished.map(strip) },
  briefing: W.briefing(deps, lists),
  layout: W.layout(input.count === undefined ? deps.length : input.count),
}));
"""


def ev(minutes_ago, status, action, session="s1", agent="Demo", **extra):
    base = 1792843200000  # NOW in ms
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp((base - minutes_ago * 60000) / 1000, tz=timezone.utc)
    e = {"schema": "acc/1", "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
         "agent": agent, "action": action, "status": status, "source": "claude-code", "host": "mac", "session": session}
    e.update(extra)
    return e


def summary(recap="Done.", ask=None, options=None, turn_minutes_ago=5):
    from datetime import datetime, timezone
    at = datetime.fromtimestamp((1792843200000 - turn_minutes_ago * 60000) / 1000, tz=timezone.utc)
    return {"steps": [], "steps_total": 0, "recap": recap, "ask": ask, "options": options or [],
            "blocked": bool(ask), "turn_at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z")}


@unittest.skipIf(NODE is None, "node is not installed")
class TestBuilding(unittest.TestCase):
    def run_model(self, events, summaries=None, count=None):
        payload = {"now": NOW, "events": events, "summaries": summaries}
        if count is not None:
            payload["count"] = count
        proc = subprocess.run([NODE, "-e", HARNESS, str(SESSIONS), str(MODEL)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def dept(self, out, name):
        return next(d for d in out["departments"] if d["name"] == name)

    def test_one_department_per_project_with_a_staffer_per_session(self):
        out = self.run_model([
            ev(3, "started", "Working", session="a1", agent="Orion"),
            ev(2, "started", "Working", session="a2", agent="Orion"),
            ev(1, "finished", "Your turn", session="b1", agent="Vega"),
        ])
        names = [d["name"] for d in out["departments"]]
        self.assertEqual(names, ["Orion", "Vega"])
        self.assertEqual(len(self.dept(out, "Orion")["staff"]), 2)
        self.assertEqual(self.dept(out, "Orion")["working"], 2)
        self.assertFalse(self.dept(out, "Orion")["inOval"])

    def test_open_permission_is_a_decision_and_the_secretary_walks_in(self):
        out = self.run_model([
            ev(4, "started", "Working", session="a1", agent="Orion"),
            ev(3, "awaiting_approval", "Needs permission", session="a1", agent="Orion",
               approval_id="x1", detail="permission: Bash", level="flash"),
        ])
        d = self.dept(out, "Orion")
        self.assertTrue(d["inOval"])
        self.assertEqual(d["decisions"], 1)
        self.assertEqual(d["staff"][0]["standing"], "decision")
        self.assertTrue(d["staff"][0]["need"].startswith("Permission:"))
        self.assertEqual([x["session"] for x in out["lists"]["decisions"]], ["a1"])

    def test_finished_turn_with_a_question_is_a_decision_without_one_an_update(self):
        events = [ev(5, "finished", "Your turn", session="q1", agent="Orion"),
                  ev(5, "finished", "Your turn", session="u1", agent="Vega")]
        out = self.run_model(events, summaries={
            "q1": summary(recap="Built it.", ask="Merge PR 13?", options=["Merge", "Not yet"]),
            "u1": summary(recap="Seat map loads fast."),
        })
        self.assertEqual(self.dept(out, "Orion")["staff"][0]["standing"], "decision")
        self.assertEqual(self.dept(out, "Orion")["staff"][0]["need"], "Merge PR 13?")
        self.assertTrue(self.dept(out, "Orion")["inOval"])
        self.assertEqual(self.dept(out, "Vega")["staff"][0]["standing"], "update")
        self.assertEqual(self.dept(out, "Vega")["staff"][0]["need"], "Nothing. For your information.")
        self.assertFalse(self.dept(out, "Vega")["inOval"])
        self.assertEqual([x["session"] for x in out["lists"]["updates"]], ["u1"])
        self.assertEqual(sorted(x["session"] for x in out["lists"]["finished"]), ["q1", "u1"])

    def test_finished_turn_without_a_summary_is_just_your_turn(self):
        out = self.run_model([ev(5, "finished", "Your turn", session="t1", agent="Orion")])
        self.assertEqual(self.dept(out, "Orion")["staff"][0]["standing"], "turn")
        self.assertIsNone(self.dept(out, "Orion")["staff"][0]["need"])
        self.assertEqual(out["lists"]["updates"], [])
        self.assertEqual(out["lists"]["finished"], [])

    def test_one_secretary_however_many_decisions_and_oldest_first(self):
        out = self.run_model([
            ev(9, "awaiting_approval", "Needs permission", session="a1", agent="Orion", approval_id="x1", detail="permission: Bash"),
            ev(2, "awaiting_approval", "Question for you", session="a2", agent="Orion", approval_id="x2", detail="question"),
            ev(5, "awaiting_approval", "Needs permission", session="b1", agent="Vega", approval_id="x3", detail="permission: Edit"),
        ])
        self.assertEqual(sum(1 for d in out["departments"] if d["inOval"]), 2)
        self.assertEqual(self.dept(out, "Orion")["decisions"], 2)
        self.assertEqual([x["session"] for x in out["lists"]["decisions"]], ["a1", "b1", "a2"])
        self.assertIn("3 decisions are waiting, brought by 2 secretaries", out["briefing"])

    def test_old_recap_is_not_finished_today(self):
        out = self.run_model([ev(60 * 30, "finished", "Your turn", session="o1", agent="Orion")],
                             summaries={"o1": summary(recap="Long ago.", turn_minutes_ago=60 * 30)})
        self.assertEqual(out["lists"]["finished"], [])
        self.assertEqual(self.dept(out, "Orion")["staff"][0]["standing"], "idle")

    def test_closed_sessions_leave_the_building(self):
        out = self.run_model([ev(10, "idle", "Session closed", session="c1", agent="Orion"),
                              ev(1, "started", "Working", session="w1", agent="Vega")])
        self.assertEqual([d["name"] for d in out["departments"]], ["Vega"])

    def test_offices_keep_a_stable_alphabetical_order(self):
        out = self.run_model([
            ev(1, "started", "Working", session="w1", agent="busy"),
            ev(1, "started", "Working", session="w2", agent="busy"),
            ev(1, "started", "Working", session="w3", agent="Calm"),
            ev(1, "awaiting_approval", "Needs permission", session="n1", agent="Zed", approval_id="x", detail="permission: Bash"),
        ])
        self.assertEqual([d["name"] for d in out["departments"]], ["busy", "Calm", "Zed"])
        # Urgency shows on the desk, not in where the office sits.
        self.assertEqual([x["session"] for x in out["lists"]["decisions"]], ["n1"])

    def test_offices_capped_and_desks_capped(self):
        events = [ev(1, "started", "Working", session="s%02d" % i, agent="P%02d" % i) for i in range(20)]
        events += [ev(1, "started", "Working", session="e%02d" % i, agent="P00") for i in range(6)]
        out = self.run_model(events)
        self.assertEqual(sum(1 for d in out["departments"] if d["drawn"]), 16)
        self.assertEqual(sum(1 for d in out["departments"] if not d["drawn"]), 4)
        self.assertEqual(self.dept(out, "P00")["desks"], 4)
        self.assertEqual(len(self.dept(out, "P00")["staff"]), 7)

    def test_quiet_briefing(self):
        out = self.run_model([ev(1, "started", "Working", session="w1", agent="Orion")])
        self.assertEqual(out["briefing"], "Mr. President, nothing needs a decision. 0 updates are for your information, and 1 staff is at work.")

    def test_layout_fills_the_top_row_then_rows_of_six(self):
        out = self.run_model([], count=4)
        lay = out["layout"]
        self.assertEqual(lay["width"], 44)
        self.assertEqual([o["x"] for o in lay["offices"]], [0, 7, 30, 37])
        self.assertTrue(all(o["door"] == "bottom" for o in lay["offices"]))
        self.assertEqual(lay["oval"], {"x": 14, "y": 0, "w": 16, "h": 12})
        self.assertEqual(lay["height"], 15)
        lay = self.run_model([], count=9)["layout"]
        self.assertEqual(len(lay["offices"]), 9)
        self.assertEqual([o["door"] for o in lay["offices"]][4:], ["top"] * 5)
        self.assertEqual(lay["height"], 27)
        # Five in the second row are centered.
        self.assertEqual(lay["offices"][4]["x"], (44 - 5 * 7) // 2)
        lay = self.run_model([], count=30)["layout"]
        self.assertEqual(len(lay["offices"]), 16)
        self.assertEqual(lay["height"], 39)


if __name__ == "__main__":
    unittest.main()
