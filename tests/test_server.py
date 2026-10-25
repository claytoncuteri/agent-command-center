"""
Tests for the multi-file event source in server.py.

Run from the repo root with the project venv (FastAPI is imported):
    python -m unittest tests.test_server
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestParseAllEvents(unittest.TestCase):
    def test_merges_files_and_tolerates_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "a.jsonl")
            b = os.path.join(tmp, "b.jsonl")
            _write(a, [{"agent": "A", "ts": "2026-01-01T00:00:00Z"}])
            _write(b, [{"agent": "B", "ts": "2026-01-01T00:00:01Z"}, {"agent": "B2", "ts": "x"}])
            events = server.parse_all_events([a, os.path.join(tmp, "missing.jsonl"), b])
            self.assertEqual([e["agent"] for e in events], ["A", "B", "B2"])

    def test_sizes_zero_for_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "a.jsonl")
            _write(a, [{"agent": "A"}])
            sizes = server._sizes([a, os.path.join(tmp, "nope.jsonl")])
            self.assertEqual(len(sizes), 2)
            self.assertGreater(sizes[0], 0)
            self.assertEqual(sizes[1], 0)


class TestLoadConfig(unittest.TestCase):
    def _load_with(self, overrides):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with open("config.json", "w", encoding="utf-8") as f:
                    json.dump(overrides, f)
                return server.load_config()
            finally:
                os.chdir(cwd)

    def test_legacy_event_file_becomes_list(self):
        cfg = self._load_with({"event_file": "./one.jsonl"})
        self.assertEqual(cfg["event_files"], ["one.jsonl"])
        self.assertEqual(cfg["event_file"], "one.jsonl")

    def test_event_files_list_expands_home_and_sets_primary(self):
        cfg = self._load_with({"event_files": ["~/x/a.jsonl", "./b.jsonl"]})
        self.assertEqual(len(cfg["event_files"]), 2)
        self.assertFalse(cfg["event_files"][0].startswith("~"))
        self.assertTrue(cfg["event_files"][0].endswith("/x/a.jsonl"))
        self.assertEqual(cfg["event_file"], cfg["event_files"][0])

    def test_event_files_string_is_accepted(self):
        cfg = self._load_with({"event_files": "./solo.jsonl"})
        self.assertEqual(cfg["event_files"], ["solo.jsonl"])

    def test_empty_list_falls_back_to_default(self):
        cfg = self._load_with({"event_files": []})
        self.assertEqual(cfg["event_files"], ["events.jsonl"])

    def test_no_config_file_uses_default(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                cfg = server.load_config()
            finally:
                os.chdir(cwd)
        self.assertEqual(cfg["event_files"], ["events.jsonl"])


class TestViews(unittest.TestCase):
    def setUp(self):
        self._view = server.CONFIG.get("view")

    def tearDown(self):
        server.CONFIG["view"] = self._view

    def test_default_view_is_sessions(self):
        server.CONFIG["view"] = "sessions"
        self.assertEqual(server.page_for(""), "sessions.html")
        self.assertEqual(server.page_for("anything/else"), "sessions.html")

    def test_classic_and_sessions_paths_always_reachable(self):
        for view in ("sessions", "classic"):
            server.CONFIG["view"] = view
            self.assertEqual(server.page_for("classic"), "index.html")
            self.assertEqual(server.page_for("/sessions/"), "sessions.html")

    def test_classic_as_default(self):
        server.CONFIG["view"] = "classic"
        self.assertEqual(server.page_for(""), "index.html")

    def test_unknown_view_falls_back(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with open("config.json", "w", encoding="utf-8") as f:
                    json.dump({"view": "nope"}, f)
                cfg = server.load_config()
            finally:
                os.chdir(cwd)
        self.assertEqual(cfg["view"], "sessions")

    def test_both_pages_exist(self):
        for name in ("index.html", "sessions.html", "sessions.js"):
            self.assertTrue((server.STATIC_DIR / name).is_file(), name)


class TestClaudeAppLinks(unittest.TestCase):
    """The desktop-app link lookup reads only sessionId and cliSessionId."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.dir = self.root / "acct" / "org"
        self.dir.mkdir(parents=True)
        server._links_cache.update({"at": 0.0, "root": None, "links": {}})
        self.t = 1_000_000.0

    def tearDown(self):
        self.tmp.cleanup()
        server._links_cache.update({"at": 0.0, "root": None, "links": {}})

    def record(self, name, body):
        path = self.dir / name
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return path

    def links(self, advance=100.0):
        self.t += advance  # past the cache window unless advance is small
        return server.claude_app_links(self.root, now=self.t)

    def test_maps_cli_prefix_to_app_link(self):
        self.record("local_1f9b14d8-71c3-4861-9f20-2068589d2c2d.json", {
            "sessionId": "local_1f9b14d8-71c3-4861-9f20-2068589d2c2d",
            "cliSessionId": "444e0301-d613-4369-a459-1e4ea874b16f",
            "permissionMode": "default", "promptAppendSnapshot": "SECRET-ish text",
        })
        self.assertEqual(self.links(), {"444e0301": "claude://claude.ai/epitaxy/local_1f9b14d8-71c3-4861-9f20-2068589d2c2d"})

    def test_rejects_bad_records(self):
        self.record("local_a.json", "{not json")
        self.record("local_b.json", {"sessionId": "javascript:alert(1)", "cliSessionId": "abcdefgh-1"})
        self.record("local_c.json", {"sessionId": "local_aaaaaaaa-1111", "cliSessionId": "short"})
        self.record("local_d.json", ["a", "list"])
        self.record("local_e.json", {"sessionId": "local_bbbbbbbb-2222"})
        self.assertEqual(self.links(), {})

    def test_oversized_record_skipped(self):
        big = {"sessionId": "local_cccccccc-3333", "cliSessionId": "cccccccc-x", "pad": "x" * 2_100_000}
        self.record("local_f.json", big)
        self.assertEqual(self.links(), {})

    def test_fork_keeps_newest_record(self):
        old = self.record("local_old.json", {"sessionId": "local_00000000-aaaa", "cliSessionId": "deadbeef-1"})
        new = self.record("local_new.json", {"sessionId": "local_11111111-bbbb", "cliSessionId": "deadbeef-1"})
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        self.assertEqual(self.links()["deadbeef"], "claude://claude.ai/epitaxy/local_11111111-bbbb")

    def test_cached_for_a_while_then_refreshed(self):
        self.assertEqual(self.links(), {})
        self.record("local_g.json", {"sessionId": "local_22222222-cccc", "cliSessionId": "feedface-1"})
        self.assertEqual(self.links(advance=5.0), {}, "within the cache window")
        self.assertIn("feedface", self.links(advance=60.0))

    def test_missing_root(self):
        self.assertEqual(server.claude_app_links(self.root / "nope", now=self.t), {})

    def test_endpoint_off_by_default(self):
        prev = server.CONFIG.get("claude_app_links")
        try:
            server.CONFIG["claude_app_links"] = False
            self.assertEqual(json.loads(server.api_links().body), {})
        finally:
            server.CONFIG["claude_app_links"] = prev


if __name__ == "__main__":
    unittest.main()
