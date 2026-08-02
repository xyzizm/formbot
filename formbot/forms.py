"""
The conversation engine.

A form is a list of questions. The bot asks them one at a time and
remembers where each user is. That is the whole model, and it is enough
for the overwhelming majority of "I need a bot that collects X" jobs.

Everything here is a pure function of (state, input) -> (state, reply).
No network, no files, no clock — so the entire conversation flow is
testable without a Telegram token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# ------------------------------------------------------------------ validators
# Adding one = one function plus one registry line.


def _v_any(value: str) -> Tuple[bool, str]:
    return (True, "") if value.strip() else (False, "Пустой ответ, попробуйте ещё раз.")


def _v_phone(value: str) -> Tuple[bool, str]:
    digits = re.sub(r"\D", "", value)
    if 10 <= len(digits) <= 15:
        return True, ""
    return False, "Не похоже на телефон. Пример: +7 999 123-45-67"


def _v_email(value: str) -> Tuple[bool, str]:
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", value.strip()):
        return True, ""
    return False, "Не похоже на email. Пример: name@example.com"


def _v_number(value: str) -> Tuple[bool, str]:
    cleaned = value.replace(" ", "").replace(",", ".")
    try:
        float(cleaned)
        return True, ""
    except ValueError:
        return False, "Нужно число. Пример: 1500"


VALIDATORS: Dict[str, Callable[[str], Tuple[bool, str]]] = {
    "any": _v_any,
    "phone": _v_phone,
    "email": _v_email,
    "number": _v_number,
}


@dataclass
class Question:
    key: str
    text: str
    validator: str = "any"
    optional: bool = False

    def validate(self, answer: str) -> Tuple[bool, str]:
        if self.optional and answer.strip() in {"-", "нет", "пропустить", "skip"}:
            return True, ""
        checker = VALIDATORS.get(self.validator)
        if checker is None:
            known = ", ".join(sorted(VALIDATORS))
            raise ValueError(
                f"Unknown validator '{self.validator}'. Available: {known}"
            )
        return checker(answer)


@dataclass
class Form:
    questions: List[Question]
    intro: str = "Здравствуйте! Задам несколько вопросов."
    done: str = "Спасибо, заявка принята. Мы свяжемся с вами."
    cancelled: str = "Отменено. Наберите /start, чтобы начать заново."

    def question_at(self, index: int) -> Optional[Question]:
        return self.questions[index] if 0 <= index < len(self.questions) else None


@dataclass
class Session:
    """Where one user is in the form, and what they have answered so far."""

    index: int = 0
    answers: Dict[str, str] = field(default_factory=dict)


@dataclass
class Reply:
    """What the bot should say, and whether the form finished on this turn."""

    text: str
    finished: bool = False
    answers: Optional[Dict[str, str]] = None


def start(form: Form) -> Tuple[Session, Reply]:
    """Begin a form. Returns a fresh session and the first question."""
    session = Session()
    first = form.question_at(0)
    if first is None:
        return session, Reply(text=form.done, finished=True, answers={})
    return session, Reply(text=f"{form.intro}\n\n{first.text}")


def advance(form: Form, session: Session, answer: str) -> Tuple[Session, Reply]:
    """
    Feed one answer in. Returns the updated session and what to say next.

    On invalid input the session does not move — the same question is asked
    again with a hint. That is what stops a bot from silently recording
    "asdf" as someone's phone number.
    """
    question = form.question_at(session.index)
    if question is None:
        # Extra message after completion — treat as a fresh start signal.
        return session, Reply(text=form.done, finished=True,
                              answers=dict(session.answers))

    ok, hint = question.validate(answer)
    if not ok:
        return session, Reply(text=f"{hint}\n\n{question.text}")

    session.answers[question.key] = answer.strip()
    session.index += 1

    next_question = form.question_at(session.index)
    if next_question is None:
        return session, Reply(text=form.done, finished=True,
                              answers=dict(session.answers))
    return session, Reply(text=next_question.text)


def build_form(config: dict) -> Form:
    """Build a Form from config, failing loudly on bad input."""
    raw_questions = config.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError("'questions' must be a non-empty list")

    questions: List[Question] = []
    seen_keys = set()
    for index, item in enumerate(raw_questions):
        where = f"questions[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        for required in ("key", "text"):
            if not item.get(required):
                raise ValueError(f"{where}: missing '{required}'")

        key = item["key"]
        if key in seen_keys:
            # Duplicate keys would silently overwrite an earlier answer.
            raise ValueError(f"{where}: duplicate key '{key}'")
        seen_keys.add(key)

        validator = item.get("validator", "any")
        if validator not in VALIDATORS:
            known = ", ".join(sorted(VALIDATORS))
            raise ValueError(
                f"{where}: unknown validator '{validator}'. Available: {known}"
            )

        questions.append(
            Question(
                key=key,
                text=item["text"],
                validator=validator,
                optional=bool(item.get("optional", False)),
            )
        )

    defaults = Form(questions=[])
    return Form(
        questions=questions,
        intro=config.get("intro", defaults.intro),
        done=config.get("done", defaults.done),
        cancelled=config.get("cancelled", defaults.cancelled),
    )


# Telegram rejects messages over 4096 characters. A rejected message means
# the admin silently never receives the lead, so we stay well under it.
TELEGRAM_MESSAGE_LIMIT = 4096
MAX_VALUE_LENGTH = 500


def escape_html(value: str) -> str:
    """
    Make user text safe for parse_mode=HTML.

    Without this, one person typing "<" in a field makes Telegram reject
    the entire admin notification — the lead is lost with no error shown
    to anyone.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_submission(answers: Dict[str, str], form: Form,
                      who: str = "") -> str:
    """
    Render answers for the admin, in the order the questions were asked.

    Every user-supplied value is escaped and length-capped, because all of
    it arrives from strangers on the internet.
    """
    lines = ["<b>Новая заявка</b>"]
    if who:
        lines.append(f"От: {escape_html(who)}")
    lines.append("")

    for question in form.questions:
        value = answers.get(question.key, "—")
        text = str(value)
        if len(text) > MAX_VALUE_LENGTH:
            text = text[:MAX_VALUE_LENGTH] + "… (обрезано)"
        lines.append(
            f"<b>{escape_html(question.text)}</b>\n{escape_html(text)}"
        )

    message = "\n".join(lines)
    if len(message) > TELEGRAM_MESSAGE_LIMIT:
        message = message[: TELEGRAM_MESSAGE_LIMIT - 20] + "\n… (обрезано)"
    return message
