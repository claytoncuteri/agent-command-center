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


if __name__ == "__main__":
    unittest.main()
