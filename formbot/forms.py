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
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

# ------------------------------------------------------------------ validators
# Adding one = one function plus one registry line.
#
# Every validator takes (value, question). Most ignore the question, but
# "choice" needs the options declared alongside it, and passing the whole
# question keeps a single signature instead of two kinds of validator.

DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d")


def _v_any(value: str, question: "Question") -> Tuple[bool, str]:
    return (True, "") if value.strip() else (False, "Пустой ответ, попробуйте ещё раз.")


def _v_phone(value: str, question: "Question") -> Tuple[bool, str]:
    digits = re.sub(r"\D", "", value)
    if 10 <= len(digits) <= 15:
        return True, ""
    return False, "Не похоже на телефон. Пример: +7 999 123-45-67"


def _v_email(value: str, question: "Question") -> Tuple[bool, str]:
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", value.strip()):
        return True, ""
    return False, "Не похоже на email. Пример: name@example.com"


def _v_number(value: str, question: "Question") -> Tuple[bool, str]:
    cleaned = value.replace(" ", "").replace(",", ".")
    try:
        float(cleaned)
        return True, ""
    except ValueError:
        return False, "Нужно число. Пример: 1500"


def match_choice(value: str, options: Tuple[str, ...]) -> Optional[str]:
    """
    Resolve an answer to one of the options, or None.

    Accepts the option text in any casing, and the 1-based number shown in
    the prompt — people reply "2" far more often than they retype the label.
    Returns the option as written in the config, so the spreadsheet gets one
    consistent spelling instead of whatever each person typed.
    """
    cleaned = value.strip()
    if not cleaned:
        return None

    if cleaned.isdigit():
        index = int(cleaned)
        if 1 <= index <= len(options):
            return options[index - 1]
        return None

    folded = cleaned.casefold()
    for option in options:
        if option.casefold() == folded:
            return option
    return None


def _v_choice(value: str, question: "Question") -> Tuple[bool, str]:
    if match_choice(value, question.options) is not None:
        return True, ""
    listed = ", ".join(question.options)
    return False, f"Выберите один из вариантов ({listed}) или пришлите его номер."


def parse_date(value: str) -> Optional[date]:
    """Parse a date in any accepted format. Returns None if it is not one."""
    import datetime

    cleaned = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _v_date(value: str, question: "Question") -> Tuple[bool, str]:
    if parse_date(value) is not None:
        return True, ""
    return False, "Не похоже на дату. Пример: 25.12.2026"


VALIDATORS: Dict[str, Callable[[str, "Question"], Tuple[bool, str]]] = {
    "any": _v_any,
    "phone": _v_phone,
    "email": _v_email,
    "number": _v_number,
    "choice": _v_choice,
    "date": _v_date,
}

# Validators that cannot work without options declared on the question.
NEEDS_OPTIONS = {"choice"}


SKIP_WORDS = {"-", "нет", "пропустить", "skip"}


@dataclass
class Question:
    key: str
    text: str
    validator: str = "any"
    optional: bool = False
    options: Tuple[str, ...] = ()

    @property
    def prompt(self) -> str:
        """
        What the user actually sees.

        Choice questions get their options numbered underneath, because an
        unlisted set of valid answers is a guessing game.
        """
        if self.validator != "choice" or not self.options:
            return self.text
        listed = "\n".join(
            f"{index}. {option}" for index, option in enumerate(self.options, 1)
        )
        return f"{self.text}\n\n{listed}"

    def is_skip(self, answer: str) -> bool:
        return self.optional and answer.strip().casefold() in SKIP_WORDS

    def validate(self, answer: str) -> Tuple[bool, str]:
        if self.is_skip(answer):
            return True, ""
        checker = VALIDATORS.get(self.validator)
        if checker is None:
            known = ", ".join(sorted(VALIDATORS))
            raise ValueError(
                f"Unknown validator '{self.validator}'. Available: {known}"
            )
        return checker(answer, self)

    def normalize(self, answer: str) -> str:
        """
        The form of the answer that gets recorded.

        A choice becomes the option as spelled in the config, and a date
        becomes ISO, so the spreadsheet holds one spelling per column no
        matter how each person typed it.
        """
        if self.is_skip(answer):
            return answer.strip()
        if self.validator == "choice":
            matched = match_choice(answer, self.options)
            return matched if matched is not None else answer.strip()
        if self.validator == "date":
            parsed = parse_date(answer)
            return parsed.isoformat() if parsed is not None else answer.strip()
        return answer.strip()


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
    return session, Reply(text=f"{form.intro}\n\n{first.prompt}")


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
        return session, Reply(text=f"{hint}\n\n{question.prompt}")

    session.answers[question.key] = question.normalize(answer)
    session.index += 1

    next_question = form.question_at(session.index)
    if next_question is None:
        return session, Reply(text=form.done, finished=True,
                              answers=dict(session.answers))
    return session, Reply(text=next_question.prompt)


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

        raw_options = item.get("options", [])
        if not isinstance(raw_options, list):
            raise ValueError(f"{where}: 'options' must be a list")
        options = tuple(str(option).strip() for option in raw_options)

        if validator in NEEDS_OPTIONS:
            # Without this the bot would start and then reject every answer,
            # which reads like a broken bot rather than a broken config.
            if len(options) < 2:
                raise ValueError(
                    f"{where}: validator '{validator}' needs at least two 'options'"
                )
            if any(not option for option in options):
                raise ValueError(f"{where}: 'options' must not contain empty values")
            folded = [option.casefold() for option in options]
            if len(set(folded)) != len(folded):
                # Duplicates would make one option unreachable by name.
                raise ValueError(f"{where}: 'options' must be unique")
        elif options:
            raise ValueError(
                f"{where}: 'options' only applies to validator "
                f"'{', '.join(sorted(NEEDS_OPTIONS))}'"
            )

        questions.append(
            Question(
                key=key,
                text=item["text"],
                validator=validator,
                optional=bool(item.get("optional", False)),
                options=options,
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
