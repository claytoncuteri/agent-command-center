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
from unittest import mock

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


PHONE = "100.64.0.10"


class TestGateDecision(unittest.TestCase):
    """Who may see the dashboard: the machine itself, and listed proxy clients only."""

    def h(self, **kw):
        return [(k.replace("_", "-").lower().encode(), v.encode()) for k, v in kw.items()]

    def test_local_browser_allowed(self):
        self.assertEqual(server.gate_decision("127.0.0.1", self.h(host="127.0.0.1:7070"), []), (True, "local"))
        for host in ("localhost:7070", "[::1]:7070", "127.0.0.1", "LOCALHOST"):
            self.assertTrue(server.gate_decision("::1", self.h(host=host), [])[0], host)

    def test_local_request_for_other_host_refused(self):
        # DNS rebinding in the local browser, or a forwarder that adds no headers.
        for host in ("evil.example.org:7070", "dash.example-tailnet.ts.net", "", "127.0.0.1.evil.example"):
            hdrs = self.h(host=host) if host else []
            self.assertFalse(server.gate_decision("127.0.0.1", hdrs, [PHONE])[0], host)

    def test_non_loopback_refused_even_without_headers(self):
        self.assertFalse(server.gate_decision("100.64.0.10", [], [PHONE])[0])
        self.assertFalse(server.gate_decision(None, [], [PHONE])[0])

    def test_listed_phone_through_proxy_allowed(self):
        hdrs = self.h(x_forwarded_for=PHONE, tailscale_user_login="me@example.org")
        self.assertEqual(server.gate_decision("127.0.0.1", hdrs, [PHONE]), (True, "remote allowed"))

    def test_other_tailnet_device_refused(self):
        hdrs = self.h(x_forwarded_for="100.64.0.20", tailscale_user_login="me@example.org")
        self.assertEqual(server.gate_decision("127.0.0.1", hdrs, [PHONE]), (False, "remote not allowed"))

    def test_proxy_without_address_refused(self):
        for hdrs in (self.h(tailscale_user_login="me@example.org"), self.h(x_forwarded_host="x.ts.net"),
                     self.h(tailscale_headers_info="https://tailscale.com/s/serve-headers"), self.h(forwarded="for=1.2.3.4")):
            self.assertFalse(server.gate_decision("127.0.0.1", hdrs, [PHONE])[0], hdrs)

    def test_address_lists_and_duplicates_refused(self):
        self.assertFalse(server.gate_decision("127.0.0.1", self.h(x_forwarded_for=PHONE + ", 6.6.6.6"), [PHONE])[0])
        two = [(b"x-forwarded-for", PHONE.encode()), (b"x-forwarded-for", b"6.6.6.6")]
        self.assertFalse(server.gate_decision("127.0.0.1", two, [PHONE])[0])
        self.assertFalse(server.gate_decision("127.0.0.1", self.h(x_forwarded_for=""), [PHONE])[0])

    def test_empty_allow_list_fails_closed(self):
        self.assertFalse(server.gate_decision("127.0.0.1", self.h(x_forwarded_for=PHONE), [])[0])
        self.assertFalse(server.gate_decision("127.0.0.1", self.h(x_forwarded_for=PHONE), None)[0])

    def test_header_names_are_case_insensitive(self):
        hdrs = [(b"X-Forwarded-For", b"100.64.0.20")]
        self.assertFalse(server.gate_decision("127.0.0.1", hdrs, [PHONE])[0])


class TestRemoteGateMiddleware(unittest.TestCase):
    """The ASGI wrapper: refused requests never reach the app, streams included."""

    def run_request(self, client, headers, allowed, scope_type="http"):
        import asyncio
        reached, sent = [], []

        async def inner(scope, receive, send):
            reached.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            sent.append(msg)

        prev = server.CONFIG.get("remote_clients")
        server.CONFIG["remote_clients"] = allowed
        try:
            scope = {"type": scope_type, "path": "/api/stream", "client": (client, 5000), "headers": headers}
            asyncio.run(server.RemoteGate(inner)(scope, receive, send))
        finally:
            server.CONFIG["remote_clients"] = prev
        status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
        return reached, status

    def test_refused_never_reaches_app(self):
        reached, status = self.run_request("127.0.0.1", [(b"x-forwarded-for", b"100.64.0.20")], [PHONE])
        self.assertEqual((reached, status), ([], 403))

    def test_websocket_from_other_device_is_closed(self):
        import asyncio
        reached, sent = [], []

        async def inner(scope, receive, send):
            reached.append(scope["type"])

        async def send(msg):
            sent.append(msg)

        prev = server.CONFIG.get("remote_clients")
        server.CONFIG["remote_clients"] = [PHONE]
        try:
            scope = {"type": "websocket", "path": "/ws", "client": ("127.0.0.1", 1),
                     "headers": [(b"x-forwarded-for", b"100.64.0.20"), (b"host", b"x.ts.net")]}
            asyncio.run(server.RemoteGate(inner)(scope, None, send))
        finally:
            server.CONFIG["remote_clients"] = prev
        self.assertEqual(reached, [])
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])

    def test_lifespan_passes_through(self):
        import asyncio
        reached = []

        async def inner(scope, receive, send):
            reached.append(scope["type"])

        asyncio.run(server.RemoteGate(inner)({"type": "lifespan"}, None, None))
        self.assertEqual(reached, ["lifespan"])

    def test_allowed_phone_reaches_stream(self):
        reached, status = self.run_request("127.0.0.1", [(b"x-forwarded-for", PHONE.encode())], [PHONE])
        self.assertEqual((reached, status), (["/api/stream"], 200))

    def test_local_reaches_app(self):
        reached, status = self.run_request("127.0.0.1", [(b"host", b"127.0.0.1:7070")], [])
        self.assertEqual((reached, status), (["/api/stream"], 200))

    def test_middleware_is_installed(self):
        self.assertTrue(any(m.cls is server.RemoteGate for m in server.app.user_middleware))

    def test_uvicorn_keeps_the_real_peer_address(self):
        # With proxy_headers on, uvicorn would wrap the app in its proxy-headers
        # middleware, replace the loopback peer with X-Forwarded-For, and the gate
        # would refuse every proxied viewer. Loaded as the LaunchAgent runs it.
        import uvicorn
        self.assertIs(server.UVICORN_OPTIONS.get("proxy_headers"), False)
        cfg = uvicorn.Config(server.app, **server.UVICORN_OPTIONS)
        cfg.load()
        self.assertIs(cfg.loaded_app.app if hasattr(cfg.loaded_app, "app") else cfg.loaded_app, server.app)
        self.assertNotIn("ProxyHeaders", type(cfg.loaded_app).__name__)
        src = pathlib.Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("**UVICORN_OPTIONS", src)


class TestRealAppThroughGate(unittest.TestCase):
    """Raw ASGI requests through the real server.app: every route sits behind the gate."""

    def call(self, path, headers, method="GET"):
        import asyncio
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "root_path": "", "headers": headers, "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 7070)}
        prev = server.CONFIG.get("remote_clients")
        server.CONFIG["remote_clients"] = [PHONE]
        try:
            asyncio.run(server.app(scope, receive, send))
        finally:
            server.CONFIG["remote_clients"] = prev
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    def test_other_device_refused_everywhere(self):
        mini = [(b"host", b"dash.example-tailnet.ts.net"), (b"x-forwarded-for", b"100.64.0.20"),
                (b"tailscale-user-login", b"me@example.org")]
        for path in ("/", "/sessions", "/classic", "/static/sessions.html", "/static/sessions.js",
                     "/apple-touch-icon.png", "/api/config", "/api/events", "/api/links", "/api/content?ref=x"):
            for method in ("GET", "HEAD"):
                self.assertEqual(self.call(path, mini, method), 403, (method, path))

    def test_phone_and_local_reach_pages(self):
        phone = [(b"host", b"dash.example-tailnet.ts.net"), (b"x-forwarded-for", PHONE.encode())]
        local = [(b"host", b"127.0.0.1:7070")]
        for hdrs in (phone, local):
            self.assertEqual(self.call("/static/sessions.js", hdrs), 200)
            self.assertEqual(self.call("/", hdrs), 200)
            self.assertEqual(self.call("/api/config", hdrs), 200)


class TestTouchIcon(unittest.TestCase):
    def setUp(self):
        self.prev = server.CONFIG.get("touch_icon")

    def tearDown(self):
        server.CONFIG["touch_icon"] = self.prev

    def test_absent_is_404(self):
        from fastapi import HTTPException
        server.CONFIG["touch_icon"] = None
        with self.assertRaises(HTTPException) as cm:
            server.touch_icon()
        self.assertEqual(cm.exception.status_code, 404)

    def test_only_png_files(self):
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as tmp:
            txt = pathlib.Path(tmp, "secret.txt"); txt.write_text("x")
            server.CONFIG["touch_icon"] = str(txt)
            with self.assertRaises(HTTPException):
                server.touch_icon()
            png = pathlib.Path(tmp, "icon.png"); png.write_bytes(b"\x89PNG\r\n\x1a\n")
            server.CONFIG["touch_icon"] = str(png)
            self.assertEqual(server.touch_icon().path, str(png))


class TestBootTime(unittest.TestCase):
    def test_boot_time_is_a_past_epoch_or_none(self):
        import time
        bt = server.boot_time()
        if bt is not None:
            self.assertGreater(bt, 1_000_000_000)
            self.assertLess(bt, time.time())

    def test_parses_macos_sysctl_format(self):
        self.assertEqual(server._BOOTTIME.search("{ sec = 1792418871, usec = 340805 } Mon Oct 19").group(1), "1792418871")

    def test_config_reports_it(self):
        body = json.loads(server.api_config().body)
        self.assertIn("booted_at", body)


class TestProjectNames(unittest.TestCase):
    def test_config_passes_clean_names_only(self):
        raw = {"Projects/Orion": "Orion", "orion-*": "Orion", "": "X", "bad": 3, "blank": "  ", "long": "n" * 90}
        with mock.patch.dict(server.CONFIG, {"project_names": raw}):
            body = json.loads(server.api_config().body)
        self.assertEqual(body["project_names"], {"Projects/Orion": "Orion", "orion-*": "Orion", "long": "n" * 40})

    def test_missing_or_wrong_type_is_empty(self):
        for value in (None, ["x"], "x"):
            with mock.patch.dict(server.CONFIG, {"project_names": value}):
                self.assertEqual(json.loads(server.api_config().body)["project_names"], {})


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
        self.assertEqual(self.links(), {"444e0301": {
            "app": "claude://claude.ai/epitaxy/local_1f9b14d8-71c3-4861-9f20-2068589d2c2d", "web": None}})

    def test_remote_control_id_gives_web_link(self):
        self.record("local_x.json", {
            "sessionId": "local_22222222-dddd", "cliSessionId": "abcdef01-2",
            "bridgeSessionIds": ["session_013EQoldOLDold", "session_014NewNewNewNew"],
        })
        self.assertEqual(self.links()["abcdef01"]["web"], "https://claude.ai/code/session_014NewNewNewNew")

    def test_bad_remote_control_ids_ignored(self):
        self.record("local_y.json", {
            "sessionId": "local_33333333-eeee", "cliSessionId": "abcdef02-3",
            "bridgeSessionIds": ["javascript:alert(1)", "session_../../x", 42, "session_short"],
        })
        self.record("local_z.json", {
            "sessionId": "local_44444444-ffff", "cliSessionId": "abcdef03-4", "bridgeSessionIds": "session_999FakeFakeFakeX",
        })
        links = self.links()
        self.assertIsNone(links["abcdef02"]["web"])
        self.assertIsNone(links["abcdef03"]["web"])
        self.assertTrue(links["abcdef02"]["app"].startswith("claude://"))

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
        self.assertEqual(self.links()["deadbeef"]["app"], "claude://claude.ai/epitaxy/local_11111111-bbbb")

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
