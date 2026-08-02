"""
Telegram Bot API client.

Deliberately small: only the calls this bot needs, wrapped so the rest of
the codebase never touches HTTP or Telegram's response envelope.

Everything returns plain Python. Failures return None or False and log a
line — a bot that crashes on a network blip is useless.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

DEFAULT_TIMEOUT = 30


class TelegramAPI:
    def __init__(self, token: str, timeout: int = DEFAULT_TIMEOUT):
        if not token:
            raise ValueError("TelegramAPI needs a bot token")
        self.token = token
        self.timeout = timeout
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def _call(self, method: str, payload: Optional[dict] = None,
              timeout: Optional[int] = None) -> Optional[Any]:
        """
        One request. Returns the `result` field, or None on any failure.

        Telegram wraps everything in {"ok": bool, "result": ...}, so the
        caller should never have to unwrap it.
        """
        try:
            response = self.session.post(
                f"{self.base}/{method}",
                json=payload or {},
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            print(f"! telegram {method}: {exc}")
            return None

        try:
            body = response.json()
        except ValueError:
            print(f"! telegram {method}: response was not JSON")
            return None

        if not body.get("ok"):
            print(
                f"! telegram {method}: {body.get('description', 'unknown error')}"
            )
            return None

        return body.get("result")

    # ---------------------------------------------------------------- calls

    def get_me(self) -> Optional[dict]:
        """Used at startup to prove the token works before polling begins."""
        return self._call("getMe")

    def get_updates(self, offset: int = 0, long_poll: int = 25) -> List[dict]:
        """
        Long-polling. The HTTP timeout must exceed the poll timeout,
        otherwise every quiet minute looks like a network error.
        """
        result = self._call(
            "getUpdates",
            {"offset": offset, "timeout": long_poll},
            timeout=long_poll + 10,
        )
        return result or []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: Optional[dict] = None,
        parse_mode: Optional[str] = "HTML",
    ) -> bool:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload) is not None

    def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        return self._call(
            "deleteMessage", {"chat_id": chat_id, "message_id": message_id}
        ) is not None

    def set_my_commands(self, commands: List[Dict[str, str]]) -> bool:
        """Populates the / menu users see in the chat."""
        return self._call("setMyCommands", {"commands": commands}) is not None
