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
        self.assertEqual(os.listdir(os.environ["ACC_STATE_DIR"]), [])

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

    def test_pre_tool_use_other_tools_ignored(self):
        evs = self.run_hook("PreToolUse", tool_name="Bash", tool_input={"command": SENTINEL})
        self.assertEqual(evs, [])

    def test_sessions_are_independent(self):
        self.run_hook("PermissionRequest", tool_name="Bash", session_id="aaaaaaaa-1")
        evs = self.run_hook("PostToolUse", tool_name="Bash", session_id="bbbbbbbb-2")
        self.assertEqual(len(evs), 1, "session B must not close session A's approval")


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
