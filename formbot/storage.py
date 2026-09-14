"""
Keeping submissions and sessions.

Two separate concerns on purpose:

  SubmissionStore — the business result. Must survive a restart.
  SessionStore    — who is mid-conversation. Cheap to lose.

Both sit behind small interfaces, so swapping the CSV for a database is
one class, not a refactor.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol

from .forms import Session


_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _spreadsheet_safe(value):
    """Keep user-controlled text from becoming a spreadsheet formula."""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if value.startswith(("\t", "\r", "\n")) or stripped.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


class SubmissionStore(Protocol):
    def add(self, answers: Dict[str, str], meta: Dict[str, str]) -> None: ...
    def count(self) -> int: ...


class CsvSubmissionStore:
    """
    Appends each submission as a row. The client opens it in Excel.

    Appending (not rewriting) means a crash mid-run cannot lose earlier
    submissions, and the file can be opened while the bot is running.
    """

    def __init__(self, path: str, columns: List[str]):
        self.path = path
        # Metadata first: the client scans for "when" and "who" before "what".
        self.columns = ["timestamp", "user_id", "username"] + list(columns)
        self._ensure_header()

    def _ensure_header(self) -> None:
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # utf-8-sig so Excel shows Cyrillic instead of mojibake.
        with open(self.path, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerow(_spreadsheet_safe(c) for c in self.columns)

    def add(self, answers: Dict[str, str], meta: Dict[str, str]) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user_id": meta.get("user_id", ""),
            "username": meta.get("username", ""),
        }
        row.update(answers)
        with open(self.path, "a", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerow(
                [_spreadsheet_safe(row.get(column, "")) for column in self.columns]
            )

    def count(self) -> int:
        if not os.path.exists(self.path):
            return 0
        with open(self.path, "r", encoding="utf-8-sig") as fh:
            return max(sum(1 for _ in fh) - 1, 0)  # minus the header

    def snapshot_with(self, submissions) -> str:
        """Prepare a complete CSV replacement for recoverable update commits."""
        with open(self.path, encoding="utf-8-sig", newline="") as fh:
            content = fh.read()
        output = io.StringIO(newline="")
        output.write(content)
        if content and not content.endswith(("\n", "\r")):
            output.write("\r\n")
        writer = csv.writer(output)
        for payload in submissions:
            meta = payload.get("meta", {})
            row = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "user_id": meta.get("user_id", ""), "username": meta.get("username", "")}
            row.update(payload.get("answers", {}))
            writer.writerow([_spreadsheet_safe(row.get(c, "")) for c in self.columns])
        return output.getvalue()


def atomic_text(path: str, content: str, encoding="utf-8") -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path + ".tmp", "w", encoding=encoding, newline="") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(path + ".tmp", path)


class UpdateOffsetStore:
    """Durable Telegram polling cursor, advanced only after local processing."""

    def __init__(self, path: str):
        self.path = path
        self.value = 0
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.value = max(int(json.load(fh).get("offset", 0)), 0)
            except (json.JSONDecodeError, OSError, AttributeError, TypeError, ValueError):
                print(f"! offset file unreadable ({path}), starting from zero")

    def advance(self, value: int) -> None:
        value = int(value)
        if value <= self.value:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"offset": value}, fh)
        os.replace(tmp, self.path)
        self.value = value


class SessionStore:
    """
    Who is mid-form, keyed by chat id.

    Persisted so a restart does not strand everyone halfway through a form
    with no way to continue.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._sessions: Dict[str, Session] = {}
        if path:
            self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for chat_id, payload in raw.items():
                self._sessions[str(chat_id)] = Session(
                    index=int(payload.get("index", 0)),
                    answers=dict(payload.get("answers", {})),
                )
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            print(f"! sessions file unreadable ({self.path}), starting fresh")

    def get(self, chat_id: int | str) -> Optional[Session]:
        return self._sessions.get(str(chat_id))

    def set(self, chat_id: int | str, session: Session) -> None:
        self._sessions[str(chat_id)] = session

    def clear(self, chat_id: int | str) -> None:
        self._sessions.pop(str(chat_id), None)

    def active(self) -> int:
        return len(self._sessions)

    def save(self) -> None:
        if not self.path:
            return
        payload = {
            chat_id: {"index": s.index, "answers": s.answers}
            for chat_id, s in self._sessions.items()
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, self.path)
