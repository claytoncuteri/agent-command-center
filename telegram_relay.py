"""
Optional Telegram relay for Agent Command Center.

Activated only when the `telegram` block is present and populated in config.json.
Sole capability: respond to /dashboard or /status commands with the dashboard URL.
No ability to trigger, modify, or act on any agent.

Requires: httpx  (pip install httpx)
"""
from __future__ import annotations

import threading
import time
from typing import Optional


class TelegramRelay:
    """Long-poll a Telegram bot and reply to /dashboard and /status commands."""

    TRIGGER_COMMANDS = {"/dashboard", "/status"}

    def __init__(self, bot_token: str, chat_id: str, dashboard_url: str) -> None:
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.dashboard_url = dashboard_url
        self._thread: Optional[threading.Thread] = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def _send(self, text: str) -> bool:
        """Send a message to the configured chat. Returns True on success."""
        try:
            import httpx
            resp = httpx.post(
                self._api_url("sendMessage"),
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as exc:
            print(f"[telegram] Send failed: {exc}")
            return False

    def _poll_loop(self) -> None:
        """Long-poll Telegram for updates and reply to recognised commands."""
        try:
            import httpx
        except ImportError:
            print("[telegram] httpx is not installed. Run: pip install httpx")
            return

        offset: int = 0
        url = self._api_url("getUpdates")

        while True:
            try:
                resp = httpx.get(
                    url,
                    params={"timeout": 30, "offset": offset},
                    timeout=35,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        msg = update.get("message", {})
                        text = msg.get("text", "").strip()
                        # Only respond to exact trigger commands
                        if text.lower().split("@")[0] in self.TRIGGER_COMMANDS:
                            self._send(f"Dashboard: {self.dashboard_url}")
                else:
                    time.sleep(5)
            except Exception as exc:
                print(f"[telegram] Poll error: {exc}")
                time.sleep(5)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the polling loop in a daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="telegram-relay")
        self._thread.start()
