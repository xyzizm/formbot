"""
Wiring: updates in, replies out.

`handle_update` is the heart of it, and it is a pure function of
(update, state) -> list of actions. It never touches the network, which
is why the whole bot can be tested with dictionaries.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .api import TelegramAPI
from .forms import Form, advance, build_form, format_submission, start
from .storage import CsvSubmissionStore, SessionStore


@dataclass
class Action:
    """Something for the caller to do. Keeps handling free of side effects."""

    kind: str  # "reply" | "notify_admin" | "save"
    chat_id: Optional[int | str] = None
    text: str = ""
    payload: Optional[Dict[str, Any]] = None


def extract_message(update: dict) -> Optional[dict]:
    """Pull the message out of an update, ignoring kinds we do not handle."""
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    if "text" not in message:
        return None  # photos, stickers, joins — not part of a text form
    return message


def describe_user(message: dict) -> Tuple[str, str]:
    """Return (user_id, human-readable name)."""
    user = message.get("from") or {}
    user_id = str(user.get("id", ""))
    username = user.get("username")
    if username:
        return user_id, f"@{username}"
    name = " ".join(
        part for part in [user.get("first_name"), user.get("last_name")] if part
    )
    return user_id, name or f"id{user_id}"


def handle_update(
    update: dict,
    form: Form,
    sessions: SessionStore,
    admin_chat_id: Optional[str] = None,
) -> List[Action]:
    """
    Turn one update into a list of actions.

    Pure: reads and writes the session store, but performs no I/O.
    """
    message = extract_message(update)
    if message is None:
        return []

    chat_id = message.get("chat", {}).get("id")
    if chat_id is None:
        return []

    text = (message.get("text") or "").strip()
    user_id, who = describe_user(message)

    # ---- commands
    if text.startswith("/"):
        command = text.split()[0].split("@")[0].lower()

        if command in ("/start", "/again"):
            session, reply = start(form)
            sessions.set(chat_id, session)
            return [Action("reply", chat_id, reply.text)]

        if command == "/cancel":
            sessions.clear(chat_id)
            return [Action("reply", chat_id, form.cancelled)]

        if command == "/help":
            return [
                Action(
                    "reply",
                    chat_id,
                    "/start — начать заново\n"
                    "/cancel — отменить\n"
                    "/help — эта справка",
                )
            ]

        # Unknown command: nudge instead of ignoring, so the user is not stuck.
        return [
            Action("reply", chat_id,
                   "Неизвестная команда. Наберите /start, чтобы начать.")
        ]

    # ---- ordinary text
    session = sessions.get(chat_id)
    if session is None:
        # Someone typed before starting. Begin the form rather than scolding.
        session, reply = start(form)
        sessions.set(chat_id, session)
        return [Action("reply", chat_id, reply.text)]

    session, reply = advance(form, session, text)

    if not reply.finished:
        sessions.set(chat_id, session)
        return [Action("reply", chat_id, reply.text)]

    # ---- form complete
    sessions.clear(chat_id)
    actions = [
        Action("reply", chat_id, reply.text),
        Action(
            "save",
            chat_id,
            payload={
                "answers": reply.answers or {},
                "meta": {"user_id": user_id, "username": who},
            },
        ),
    ]
    if admin_chat_id:
        actions.append(
            Action(
                "notify_admin",
                admin_chat_id,
                format_submission(reply.answers or {}, form, who),
            )
        )
    return actions


class Bot:
    """Polls Telegram and performs the actions handle_update returns."""

    def __init__(self, config: dict):
        token = config.get("bot_token")
        if not token:
            raise SystemExit("Config is missing 'bot_token'")

        self.api = TelegramAPI(token)
        self.form = build_form(config)
        self.admin_chat_id = config.get("admin_chat_id")

        self.sessions = SessionStore(config.get("sessions_path", "sessions.json"))
        self.submissions = CsvSubmissionStore(
            config.get("submissions_path", "submissions.csv"),
            [question.key for question in self.form.questions],
        )
        self.long_poll = int(config.get("long_poll_seconds", 25))

    def perform(self, action: Action) -> None:
        if action.kind == "reply" or action.kind == "notify_admin":
            self.api.send_message(action.chat_id, action.text)
        elif action.kind == "save":
            payload = action.payload or {}
            self.submissions.add(payload.get("answers", {}),
                                 payload.get("meta", {}))

    def run(self) -> None:
        me = self.api.get_me()
        if me is None:
            raise SystemExit(
                "Could not reach Telegram — check the bot token and connection."
            )
        print(f"FormBot running as @{me.get('username')}")
        print(f"Submissions so far: {self.submissions.count()}")
        print("Ctrl+C to stop.\n")

        self.api.set_my_commands([
            {"command": "start", "description": "начать заново"},
            {"command": "cancel", "description": "отменить"},
            {"command": "help", "description": "справка"},
        ])

        offset = 0
        while True:
            try:
                updates = self.api.get_updates(offset, self.long_poll)
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        for action in handle_update(
                            update, self.form, self.sessions, self.admin_chat_id
                        ):
                            self.perform(action)
                    except Exception as exc:
                        # One malformed update must not kill the bot.
                        print(f"! update {update.get('update_id')} failed: "
                              f"{type(exc).__name__}: {exc}")
                self.sessions.save()
            except KeyboardInterrupt:
                print("\nStopped.")
                self.sessions.save()
                return
            except Exception as exc:
                print(f"! poll error: {exc}")
                time.sleep(5)


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(
            f"Config not found: {path}\n"
            "Copy config.example.json to config.json and fill it in."
        )
    with open(path, "r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Config is not valid JSON: {exc}") from None
