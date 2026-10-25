"""
Tests for examples/claude_code_hook.py.

Run from the repo root:  python -m unittest tests.test_claude_code_hook
Stdlib only. Every test writes to its own temp directory via ACC_EVENT_FILE.
"""
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK_PATH = ROOT / "examples" / "claude_code_hook.py"

SENTINEL = "SENTINEL-DO-NOT-LEAK-9f3a1c"


def _load_hook():
    spec = importlib.util.spec_from_file_location("acc_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.event_file = os.path.join(self.tmp.name, "events.jsonl")
        self.env = mock.patch.dict(
            os.environ,
            {
                "ACC_EVENT_FILE": self.event_file,
                "ACC_STATE_DIR": os.path.join(self.tmp.name, "state"),
                "ACC_HOST": "testhost",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    # helpers
    def run_hook(self, event, session_id="sess-0001-abcdef", cwd="/Users/x/Projects/Demo", **extra):
        data = {"hook_event_name": event, "session_id": session_id, "cwd": cwd}
        data.update(extra)
        out = io.StringIO()
        with redirect_stdout(out):
            hook.handle(data)
        self.assertEqual(out.getvalue(), "", "hook must print nothing")
        return self.events()

    def events(self):
        if not os.path.exists(self.event_file):
            return []
        with open(self.event_file, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def raw(self):
        if not os.path.exists(self.event_file):
            return ""
        with open(self.event_file, encoding="utf-8") as f:
            return f.read()


class TestLifecycle(HookCase):
    def test_session_start_opened(self):
        evs = self.run_hook("SessionStart", source="startup")
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual(e["schema"], "acc/1")
        self.assertEqual(e["status"], "idle")
        self.assertEqual(e["action"], "Session opened")
        self.assertEqual(e["agent"], "Demo")
        self.assertEqual(e["host"], "testhost")
        self.assertEqual(e["session"], "sess-000")
        self.assertEqual(e["source"], "claude-code")
        self.assertTrue(e["ts"].endswith("Z"))

    def test_session_start_resumed(self):
        evs = self.run_hook("SessionStart", source="resume")
        self.assertEqual(evs[0]["action"], "Session resumed")

    def test_prompt_stop_end(self):
        self.run_hook("UserPromptSubmit")
        self.run_hook("Stop")
        evs = self.run_hook("SessionEnd", reason="other")
        self.assertEqual([e["status"] for e in evs], ["started", "finished", "idle"])
        self.assertEqual([e["action"] for e in evs], ["Working", "Your turn", "Session closed"])

    def test_unknown_event_writes_nothing(self):
        evs = self.run_hook("SubagentStop")
        self.assertEqual(evs, [])

    def test_missing_event_name_writes_nothing(self):
        hook.handle({"session_id": "x"})
        self.assertEqual(self.events(), [])


class TestApprovals(HookCase):
    def test_permission_request_then_post_tool_use_approves(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("PostToolUse", tool_name="Bash")
        self.assertEqual(len(evs), 2)
        opened, closed = evs
        self.assertEqual(opened["status"], "awaiting_approval")
        self.assertEqual(opened["level"], "flash")
        self.assertEqual(opened["action"], "Needs permission")
        self.assertEqual(opened["detail"], "permission: Bash")
        self.assertEqual(closed["status"], "approved")
        self.assertEqual(closed["approval_id"], opened["approval_id"])
        self.assertTrue(opened["approval_id"].startswith("sess-000-"))

    def test_post_tool_use_for_other_tool_does_not_resolve(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("PostToolUse", tool_name="Read")
        self.assertEqual(len(evs), 1, "an unrelated tool must not close the approval")

    def test_post_tool_use_without_open_approval_writes_nothing(self):
        evs = self.run_hook("PostToolUse", tool_name="Read")
        self.assertEqual(evs, [])

    def test_duplicate_request_same_tool_written_once(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("PermissionRequest", tool_name="Bash")
        self.assertEqual(len(evs), 1)

    def test_new_request_supersedes_old(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("PermissionRequest", tool_name="Edit")
        self.assertEqual([e["status"] for e in evs], ["awaiting_approval", "declined", "awaiting_approval"])
        self.assertEqual(evs[1]["result"], "superseded")
        self.assertEqual(evs[1]["approval_id"], evs[0]["approval_id"])
        self.assertNotEqual(evs[2]["approval_id"], evs[0]["approval_id"])

    def test_permission_denied_declines(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("PermissionDenied", tool_name="Bash")
        self.assertEqual(evs[-1]["status"], "declined")
        self.assertEqual(evs[-1]["approval_id"], evs[0]["approval_id"])

    def test_failure_after_approval_counts_as_approved(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("PostToolUseFailure", tool_name="Bash", is_interrupt=False)
        self.assertEqual(evs[-1]["status"], "approved")
        self.assertEqual(evs[-1]["result"], "tool failed")

    def test_interrupt_declines(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("PostToolUseFailure", tool_name="Bash", is_interrupt=True)
        self.assertEqual(evs[-1]["status"], "declined")
        self.assertEqual(evs[-1]["result"], "interrupted")

    def test_open_approval_cleared_at_next_prompt(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("UserPromptSubmit")
        self.assertEqual([e["status"] for e in evs], ["awaiting_approval", "declined", "started"])
        self.assertEqual(evs[1]["result"], "not run")

    def test_open_approval_cleared_at_stop(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("Stop")
        self.assertEqual([e["status"] for e in evs], ["awaiting_approval", "declined", "finished"])

    def test_open_approval_cleared_at_session_end_and_state_removed(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("SessionEnd", reason="other")
        self.assertEqual([e["status"] for e in evs], ["awaiting_approval", "declined", "idle"])
        # The open-ask state is gone; only the session's tiny lock file stays.
        self.assertEqual([n for n in os.listdir(os.environ["ACC_STATE_DIR"]) if not n.endswith(".lock")], [])

    def test_resume_clears_a_prompt_lost_in_a_restart(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("SessionStart", source="resume")
        self.assertEqual([e["status"] for e in evs], ["awaiting_approval", "declined", "idle"])
        self.assertEqual(evs[1]["approval_id"], evs[0]["approval_id"])
        self.assertEqual(evs[1]["result"], "not run")

    def test_compact_keeps_an_open_prompt(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("SessionStart", source="compact")
        self.assertEqual([e["status"] for e in evs], ["awaiting_approval", "idle"])

    def test_fresh_session_start_drops_stale_state(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        self.run_hook("SessionStart", source="startup")
        evs = self.run_hook("PostToolUse", tool_name="Bash")
        # The stale approval was forgotten at startup, so PostToolUse writes nothing.
        self.assertEqual([e["status"] for e in evs], ["awaiting_approval", "idle"])

    def test_notification_fallback_only_when_nothing_open(self):
        evs = self.run_hook("Notification", notification_type="permission_prompt", message=SENTINEL)
        self.assertEqual(evs[0]["status"], "awaiting_approval")
        self.assertEqual(evs[0]["detail"], "permission")
        evs = self.run_hook("Notification", notification_type="permission_prompt", message=SENTINEL)
        self.assertEqual(len(evs), 1)
        # Any tool resolves a notification-opened approval (it carries no tool name).
        evs = self.run_hook("PostToolUse", tool_name="Grep")
        self.assertEqual(evs[-1]["status"], "approved")

    def test_other_notification_types_ignored(self):
        for kind in ("idle_prompt", "auth_success", "elicitation_dialog", "agent_completed"):
            self.run_hook("Notification", notification_type=kind, message=SENTINEL)
        self.assertEqual(self.events(), [])

    def test_question_via_pre_tool_use(self):
        self.run_hook("PreToolUse", tool_name="AskUserQuestion", tool_input={"q": SENTINEL})
        evs = self.run_hook("PostToolUse", tool_name="AskUserQuestion", tool_response={"a": SENTINEL})
        self.assertEqual(evs[0]["action"], "Question for you")
        self.assertEqual(evs[0]["detail"], "question")
        self.assertEqual(evs[1]["status"], "approved")
        self.assertEqual(evs[1]["result"], "answered")

    def test_pre_tool_use_records_doing_only_on_change(self):
        self.run_hook("PreToolUse", tool_name="Edit", tool_input={"file_path": SENTINEL})
        self.run_hook("PreToolUse", tool_name="Write")  # same activity: no new event
        evs = self.run_hook("PreToolUse", tool_name="Bash", tool_input={"command": SENTINEL})
        self.assertEqual([e.get("doing") for e in evs], ["Editing files", "Running a command"])
        self.assertTrue(all(e["status"] == "started" and e["action"] == "Working" for e in evs))
        self.assertNotIn(SENTINEL, self.raw())

    def test_doing_resets_each_turn(self):
        self.run_hook("UserPromptSubmit")
        self.run_hook("PreToolUse", tool_name="Read")
        self.run_hook("Stop")
        self.run_hook("UserPromptSubmit")
        evs = self.run_hook("PreToolUse", tool_name="Read")
        self.assertEqual(evs[-1].get("doing"), "Reading files", "a new turn reports Reading again")
        self.assertEqual(evs[0].get("doing"), "Thinking")


class TestPlainLanguage(HookCase):
    def test_doing_labels(self):
        cases = {
            "Edit": "Editing files", "Grep": "Searching the code", "Bash": "Running a command",
            "WebSearch": "Browsing the web", "Task": "Running a sub-agent", "Read": "Reading files",
            "mcp__claude-in-chrome__navigate": "Using the browser",
            "mcp__1a59c906-04da-521d-bda7-7f71b9f9e01c__batch": "Using a connector",
            "mcp__github__create_pr": "Using github",
            "mcp__ccd_turn__update_plan": "Updating its task list",
            "mcp__ccd_session__start_session": "Managing sessions",
            "mcp__ccd_view__show_pane": "Using the app",
            "Artifact": "Publishing a page",
            "SomethingNew": "Using SomethingNew",
            "": "Working",
        }
        for tool, label in cases.items():
            self.assertEqual(hook.doing_label(tool), label, tool)

    def test_resolution_actions_never_repeat_the_ask(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        self.run_hook("PostToolUse", tool_name="Bash")
        self.run_hook("PermissionRequest", tool_name="Edit")
        self.run_hook("PermissionDenied", tool_name="Edit")
        self.run_hook("PreToolUse", tool_name="AskUserQuestion")
        self.run_hook("PostToolUse", tool_name="AskUserQuestion")
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("Stop")
        resolved = [e["action"] for e in evs if e["status"] in ("approved", "declined")]
        self.assertEqual(
            resolved, ["Permission granted", "Permission denied", "Question answered", "Prompt dismissed"]
        )

    def test_interrupt_label(self):
        self.run_hook("PermissionRequest", tool_name="Bash")
        evs = self.run_hook("PostToolUseFailure", tool_name="Bash", is_interrupt=True)
        self.assertEqual(evs[-1]["action"], "Interrupted")

    def test_session_title_captured_and_cleaned(self):
        evs = self.run_hook("SessionStart", source="startup", session_title="  Website:\n Build  page ")
        self.assertEqual(evs[0]["title"], "Website: Build page")
        evs = self.run_hook("SessionStart", source="resume", session_title="X" * 300)
        self.assertEqual(len(evs[-1]["title"]), hook.TITLE_MAX)

    def test_no_title_field_when_absent(self):
        evs = self.run_hook("SessionStart", source="startup")
        self.assertNotIn("title", evs[0])
        evs = self.run_hook("SessionStart", source="startup", session_title=123)
        self.assertNotIn("title", evs[-1])

    def test_sessions_are_independent(self):
        self.run_hook("PermissionRequest", tool_name="Bash", session_id="aaaaaaaa-1")
        evs = self.run_hook("PostToolUse", tool_name="Bash", session_id="bbbbbbbb-2")
        self.assertEqual(len(evs), 1, "session B must not close session A's approval")


PLAN = "mcp__ccd_turn__update_plan"


class TestStepProgress(HookCase):
    """Counts from task lists, never their text."""

    def items(self, *statuses):
        return {"items": [{"content": SENTINEL + str(i), "status": s} for i, s in enumerate(statuses)], "scope": "x"}

    def steps(self, evs):
        return [(e["steps_done"], e["steps_total"], e.get("steps_current")) for e in evs if "steps_total" in e]

    def test_app_plan_items_advance_and_wrap_up(self):
        self.run_hook("PostToolUse", tool_name=PLAN, tool_input=self.items("in_progress", "pending", "pending"))
        self.run_hook("PostToolUse", tool_name=PLAN, tool_input={"advance": True})
        self.run_hook("PostToolUse", tool_name=PLAN, tool_input={"advance": True})
        evs = self.run_hook("PostToolUse", tool_name="mcp__ccd_turn__wrap_up", tool_input={"recap": SENTINEL})
        self.assertEqual(self.steps(evs), [(0, 3, 1), (1, 3, 2), (2, 3, 3), (3, 3, None)])
        self.assertNotIn(SENTINEL, self.raw())

    def test_app_plan_done_and_start(self):
        self.run_hook("PostToolUse", tool_name=PLAN, tool_input=self.items("in_progress", "pending", "pending", "pending"))
        evs = self.run_hook("PostToolUse", tool_name=PLAN, tool_input={"done": [1, 2], "start": 4})
        self.assertEqual(self.steps(evs)[-1], (2, 4, 4))

    def test_terminal_todowrite(self):
        todos = {"todos": [{"content": SENTINEL, "activeForm": SENTINEL, "status": s}
                           for s in ("completed", "completed", "in_progress", "pending")]}
        evs = self.run_hook("PostToolUse", tool_name="TodoWrite", tool_input=todos)
        self.assertEqual(self.steps(evs), [(2, 4, 3)])
        self.assertNotIn(SENTINEL, self.raw())

    def test_no_event_when_nothing_changed(self):
        self.run_hook("PostToolUse", tool_name=PLAN, tool_input=self.items("in_progress", "pending"))
        evs = self.run_hook("PostToolUse", tool_name=PLAN, tool_input=self.items("in_progress", "pending"))
        self.assertEqual(len(self.steps(evs)), 1)

    def test_progress_without_a_list_is_ignored(self):
        evs = self.run_hook("PostToolUse", tool_name=PLAN, tool_input={"advance": True})
        evs = self.run_hook("PostToolUse", tool_name="mcp__ccd_turn__wrap_up", tool_input={})
        self.assertEqual(self.steps(evs), [])

    def test_other_tools_and_bad_inputs_ignored(self):
        self.run_hook("PostToolUse", tool_name="Bash", tool_input={"items": [{"status": "completed"}]})
        self.run_hook("PostToolUse", tool_name=PLAN, tool_input={"items": "not a list"})
        self.run_hook("PostToolUse", tool_name=PLAN, tool_input=None)
        self.run_hook("PostToolUse", tool_name="mcp__other__update_plan", tool_input=self.items("pending"))
        self.assertEqual(self.steps(self.events()), [])

    def test_unknown_statuses_count_as_pending_and_list_is_capped(self):
        evs = self.run_hook("PostToolUse", tool_name=PLAN,
                            tool_input={"items": [{"status": "weird"}] * 80})
        self.assertEqual(self.steps(evs), [(0, hook._MAX_STEPS, 1)])

    def test_unit_helpers(self):
        self.assertEqual(hook.current_step(["completed", "completed"]), None)
        self.assertEqual(hook.current_step(["completed", "pending"]), 2)
        self.assertEqual(hook.update_steps(["in_progress"], PLAN, {"start": 9}), ["in_progress"])


class TestPauseRequests(HookCase):
    """Opt-in: a prompt asking the session to pause sets a flag, never writes text."""

    ASKS = [
        "i need to close my laptop soon. please pause",
        "pause on this for now lol",
        "Pause.",
        "ok pause here",
        "please find a good stopping point",
        "I'm shutting down my MacBook, stop for now",
        "let's wrap up for today",
        "Pause for the night please",
        "can you pause while i walk through the airport and we pick up later? No rush",
        "let's pause until tonight",
        "could you please pause, I'll resume tomorrow",
        "we can pick this up later",
    ]
    NOT_ASKS = [
        "add a pause button to the player",
        "when the host pauses the movie, sync everyone",
        "pause the video for everyone in the room",
        "can you make the pause icon gold",
        "the stream stops for now and then, why?",
        "fix the paused state on the watch party screen",
        "x" * 700 + " please pause",
        "can you pause the video for everyone when the host leaves?",
        "let's pause the movie automatically on disconnect",
        "could you pause the player when the app backgrounds",
        "the host can pause playback at any time",
    ]

    def setUp(self):
        super().setUp()
        os.environ["ACC_DETECT_PAUSE"] = "1"

    def test_time_limits_are_not_pauses(self):
        cases = {"you have 10 minutes to complete this task, if you have not completed, then please pause": 600,
                 "yes please, if you can do it in 10 min": 600, "pause in 15 minutes": 900, "you have 2 hours": 7200}
        for text, secs in cases.items():
            self.assertEqual(hook.time_limit_seconds(text), secs, text)
            self.assertFalse(hook.is_pause_request(text), text)
        evs = self.run_hook("UserPromptSubmit", prompt="you have 10 minutes, then please pause " + SENTINEL)
        self.assertEqual(evs[-1].get("time_limit"), 600)
        self.assertNotIn("pause", evs[-1])
        evs = self.run_hook("Stop")
        self.assertNotIn("pause", evs[-1])
        self.assertNotIn(SENTINEL, self.raw())

    def test_real_phrasings_detected(self):
        for text in self.ASKS:
            self.assertTrue(hook.is_pause_request(text), text)

    def test_app_talk_and_long_prompts_ignored(self):
        for text in self.NOT_ASKS:
            self.assertFalse(hook.is_pause_request(text), text[:60])

    def test_off_by_default(self):
        os.environ.pop("ACC_DETECT_PAUSE")
        self.assertFalse(hook.is_pause_request("please pause"))
        evs = self.run_hook("UserPromptSubmit", prompt="please pause")
        self.assertNotIn("pause", evs[-1])

    def test_flag_on_prompt_and_stop_then_cleared(self):
        marker = "please pause " + SENTINEL
        self.run_hook("UserPromptSubmit", prompt=marker)
        self.run_hook("PreToolUse", tool_name="Edit")
        evs = self.run_hook("Stop")
        self.assertTrue(evs[0]["pause"])
        self.assertEqual(evs[0]["doing"], "Thinking")
        self.assertTrue(evs[-1]["pause"])
        self.assertEqual(evs[-1]["status"], "finished")
        evs = self.run_hook("UserPromptSubmit", prompt="ok carry on")
        self.assertNotIn("pause", evs[-1])
        evs = self.run_hook("Stop")
        self.assertNotIn("pause", evs[-1])
        self.assertNotIn(SENTINEL, self.raw())
        self.assertNotIn("please pause", self.raw())


class TestQueuedPauseRequests(HookCase):
    """A pause sent while the session works arrives queued; it is spotted from the history file."""

    def setUp(self):
        super().setUp()
        os.environ["ACC_DETECT_PAUSE"] = "1"
        self.home = os.path.join(self.tmp.name, "home")
        self.proj = os.path.join(self.home, ".claude", "projects", "-p")
        os.makedirs(self.proj)
        self.home_patch = mock.patch.dict(os.environ, {"HOME": self.home})
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        super().tearDown()

    def history(self, *recs):
        path = os.path.join(self.proj, "s.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        return path

    @staticmethod
    def typed(text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    @staticmethod
    def queued(text, kind="human"):
        return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text,
                                                       "origin": {"kind": kind}, "commandMode": "prompt"}}

    def test_queued_pause_marks_early_then_paused_at_stop(self):
        path = self.history(self.typed("build the lobby " + SENTINEL),
                            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
                            self.queued("i need to close my laptop soon. please pause"))
        self.run_hook("UserPromptSubmit", prompt="build the lobby")
        evs = self.run_hook("PostToolUse", tool_name="Bash", transcript_path=path)
        self.assertEqual([e["action"] for e in evs if e.get("pause")], ["Pause requested"])
        self.run_hook("PostToolUse", tool_name="Edit", transcript_path=path)
        evs = self.run_hook("Stop", transcript_path=path)
        self.assertEqual(len([e for e in evs if e["action"] == "Pause requested"]), 1, "marked once")
        self.assertTrue(evs[-1]["pause"])
        self.assertNotIn(SENTINEL, self.raw())
        self.assertNotIn("close my laptop", self.raw())

    def test_stop_alone_catches_a_queued_pause(self):
        path = self.history(self.typed("go"), self.queued("pause on this for now lol"))
        evs = self.run_hook("Stop", transcript_path=path)
        self.assertTrue(evs[-1]["pause"])

    def test_notes_from_other_sessions_do_not_count(self):
        path = self.history(self.typed("go"), self.queued("<task-notification>please pause</task-notification>", kind="task-notification"),
                            self.queued("please pause", kind="peer"))
        evs = self.run_hook("Stop", transcript_path=path)
        self.assertNotIn("pause", evs[-1])

    def test_a_newer_message_wins(self):
        path = self.history(self.queued("please pause"), self.typed("actually keep going"))
        evs = self.run_hook("Stop", transcript_path=path)
        self.assertNotIn("pause", evs[-1])

    def test_off_by_default_reads_nothing(self):
        os.environ.pop("ACC_DETECT_PAUSE")
        path = self.history(self.queued("please pause"))
        with mock.patch.object(hook, "latest_human_message", side_effect=AssertionError("must not read")):
            evs = self.run_hook("Stop", transcript_path=path)
            self.run_hook("PostToolUse", tool_name="Bash", transcript_path=path)
        self.assertNotIn("pause", evs[-1])

    def test_outside_claude_projects_refused(self):
        outside = os.path.join(self.tmp.name, "x.jsonl")
        with open(outside, "w") as f:
            f.write(json.dumps(self.queued("please pause")) + "\n")
        self.assertIsNone(hook.latest_human_message(outside))


class TestConcurrency(HookCase):
    """Hooks that fire at the same instant must not both open an ask."""

    def test_simultaneous_permission_request_and_pre_tool_use(self):
        import threading
        env = dict(os.environ)
        payloads = [
            {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion", "session_id": "race0001-x", "cwd": "/p/Demo"},
            {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": "race0001-x", "cwd": "/p/Demo"},
        ]
        for _round in range(25):
            if os.path.exists(self.event_file):
                os.remove(self.event_file)
            for name in os.listdir(os.environ["ACC_STATE_DIR"]) if os.path.isdir(os.environ["ACC_STATE_DIR"]) else []:
                if name.endswith(".json"):
                    os.remove(os.path.join(os.environ["ACC_STATE_DIR"], name))
            procs = [subprocess.Popen([sys.executable, str(HOOK_PATH)], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, env=env) for _ in payloads]
            barrier = threading.Barrier(len(procs))

            def feed(p, data):
                barrier.wait()
                p.communicate(json.dumps(data).encode(), timeout=20)

            threads = [threading.Thread(target=feed, args=(p, d)) for p, d in zip(procs, payloads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            opened = [e for e in self.events() if e["status"] == "awaiting_approval"]
            self.assertEqual(len(opened), 1, "round %d opened %d asks" % (_round, len(opened)))

    def test_lock_file_lives_beside_state(self):
        self.run_hook("UserPromptSubmit", session_id="lockcheck-1")
        names = os.listdir(os.environ["ACC_STATE_DIR"])
        self.assertTrue(any(n.endswith(".lock") for n in names), names)


class TestPrivacy(HookCase):
    def test_sentinel_never_reaches_the_file(self):
        poison = {
            "prompt": SENTINEL,
            "tool_input": {"command": SENTINEL, "file_path": SENTINEL},
            "tool_response": {"stdout": SENTINEL},
            "last_assistant_message": SENTINEL,
            "message": SENTINEL,
            "title": SENTINEL,
            "transcript_path": "/tmp/" + SENTINEL,
            "error": SENTINEL,
            "permission_suggestions": [SENTINEL],
        }
        self.run_hook("SessionStart", source="startup", **poison)
        self.run_hook("UserPromptSubmit", **poison)
        self.run_hook("PermissionRequest", tool_name="Bash", **poison)
        self.run_hook("Notification", notification_type="permission_prompt", **poison)
        self.run_hook("PostToolUse", tool_name="Bash", **poison)
        self.run_hook("PreToolUse", tool_name="AskUserQuestion", **poison)
        self.run_hook("PostToolUseFailure", tool_name="AskUserQuestion", is_interrupt=True, **poison)
        self.run_hook("PermissionDenied", tool_name="Bash", **poison)
        self.run_hook("Stop", **poison)
        self.run_hook("SessionEnd", reason="other", **poison)
        self.assertNotIn(SENTINEL, self.raw())
        self.assertGreater(len(self.events()), 5)

    def test_long_tool_name_is_capped(self):
        self.run_hook("PermissionRequest", tool_name="X" * 500)
        e = self.events()[0]
        self.assertLessEqual(len(e["detail"]), hook.DETAIL_MAX)
        self.assertLess(len(self.raw().encode()), hook.MAX_LINE_BYTES)

    def test_file_permissions(self):
        self.run_hook("SessionStart", source="startup")
        self.assertEqual(os.stat(self.event_file).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.event_file)).st_mode & 0o777 & 0o077, 0,
                         "event directory must not be group/world accessible")


class TestTitlesFromTranscript(HookCase):
    """The opt-in title reader: only custom-title lines, only under ~/.claude/projects."""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp.name, "home")
        self.proj = os.path.join(self.home, ".claude", "projects", "-p")
        os.makedirs(self.proj)
        self.home_patch = mock.patch.dict(os.environ, {"HOME": self.home})
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        super().tearDown()

    def transcript(self, lines, name="s.jsonl", where=None):
        path = os.path.join(where or self.proj, name)
        with open(path, "w", encoding="utf-8") as f:
            for rec in lines:
                f.write((rec if isinstance(rec, str) else json.dumps(rec)) + "\n")
        return path

    def poison_lines(self):
        return [
            {"type": "user", "message": {"content": SENTINEL}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": SENTINEL}]}},
        ]

    def test_off_by_default(self):
        path = self.transcript([{"type": "custom-title", "customTitle": "Hidden", "sessionId": "x"}])
        evs = self.run_hook("Stop", transcript_path=path)
        self.assertNotIn("title", evs[-1])

    def test_latest_title_on_stop_and_start(self):
        os.environ["ACC_READ_TITLES"] = "1"
        path = self.transcript(self.poison_lines() + [
            {"type": "custom-title", "customTitle": "First name", "sessionId": "x"},
            {"type": "user", "message": {"content": SENTINEL}},
            {"type": "custom-title", "customTitle": "Website: Build the /contact page", "sessionId": "x"},
        ])
        evs = self.run_hook("Stop", transcript_path=path)
        self.assertEqual(evs[-1]["title"], "Website: Build the /contact page")
        evs = self.run_hook("SessionStart", source="resume", transcript_path=path)
        self.assertEqual(evs[-1]["title"], "Website: Build the /contact page")
        self.assertNotIn(SENTINEL, self.raw())

    def test_title_found_before_the_tail_window(self):
        os.environ["ACC_READ_TITLES"] = "1"
        filler = [{"type": "assistant", "message": {"content": "x" * 2000}}] * 400  # about 800 KB
        path = self.transcript([{"type": "custom-title", "customTitle": "Early title", "sessionId": "x"}] + filler)
        self.assertGreater(os.path.getsize(path), hook._TITLE_TAIL_BYTES)
        self.assertEqual(hook.title_from_transcript(path), "Early title")

    def test_refuses_files_outside_claude_projects(self):
        os.environ["ACC_READ_TITLES"] = "1"
        outside = self.transcript(
            [{"type": "custom-title", "customTitle": "Nope", "sessionId": "x"}], where=self.tmp.name
        )
        self.assertIsNone(hook.title_from_transcript(outside))
        sneaky = os.path.join(self.proj, "..", "..", "..", os.path.basename(outside))
        self.assertIsNone(hook.title_from_transcript(sneaky))
        not_jsonl = self.transcript([{"type": "custom-title", "customTitle": "Nope"}], name="s.txt")
        self.assertIsNone(hook.title_from_transcript(not_jsonl))

    def test_quoted_title_text_inside_a_message_is_ignored(self):
        os.environ["ACC_READ_TITLES"] = "1"
        quoted = {"type": "user", "message": {"content": '{"type":"custom-title","customTitle":"' + SENTINEL + '"}'}}
        path = self.transcript([quoted], name="q.jsonl")
        with open(path, "rb") as f:
            self.assertFalse(any(m in f.read() for m in hook._TITLE_MARKS))
        self.assertIsNone(hook.title_from_transcript(path))

    def test_missing_file_and_garbage_lines(self):
        os.environ["ACC_READ_TITLES"] = "1"
        self.assertIsNone(hook.title_from_transcript(os.path.join(self.proj, "missing.jsonl")))
        path = self.transcript(['{"type":"custom-title", broken', "not json at all"])
        self.assertIsNone(hook.title_from_transcript(path))
        evs = self.run_hook("Stop", transcript_path=path)
        self.assertNotIn("title", evs[-1])


class TestAgentName(unittest.TestCase):
    def test_plain_folder(self):
        self.assertEqual(hook.agent_name("/Users/x/Projects/Orion"), "Orion")

    def test_worktree_maps_to_repo(self):
        self.assertEqual(
            hook.agent_name("/Users/x/Projects/Site/.claude/worktrees/contact-page-70f4bb"), "Site"
        )

    def test_scratch_collapses(self):
        self.assertEqual(hook.agent_name("/x/scratch-workspaces/a/b/scratch-2026-10-24-5f99bb"), "scratch")

    def test_empty(self):
        self.assertEqual(hook.agent_name(""), "unknown")


class TestProcess(unittest.TestCase):
    """Run the script as Claude Code would: JSON on stdin, exit 0, silent."""

    def _run(self, stdin_text, env_extra):
        env = dict(os.environ)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def test_valid_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            ef = os.path.join(tmp, "events.jsonl")
            payload = json.dumps(
                {"hook_event_name": "UserPromptSubmit", "session_id": "abc12345-z", "cwd": "/p/Demo", "prompt": SENTINEL}
            )
            proc = self._run(payload, {"ACC_EVENT_FILE": ef, "ACC_HOST": "h"})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertEqual(proc.stderr, "")
            with open(ef, encoding="utf-8") as f:
                text = f.read()
            self.assertIn('"status":"started"', text)
            self.assertNotIn(SENTINEL, text)

    def test_bad_json_exits_zero_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ef = os.path.join(tmp, "events.jsonl")
            proc = self._run("{not json", {"ACC_EVENT_FILE": ef})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertFalse(os.path.exists(ef))

    def test_empty_stdin_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run("", {"ACC_EVENT_FILE": os.path.join(tmp, "e.jsonl")})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

    def test_unwritable_event_dir_still_exits_zero(self):
        proc = self._run(
            json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p"}),
            {"ACC_EVENT_FILE": "/dev/null/impossible/events.jsonl"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
