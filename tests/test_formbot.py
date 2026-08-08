"""
Tests for FormBot.

    python3 tests/test_formbot.py

No Telegram token needed — updates are plain dictionaries and the
conversation engine is pure.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formbot.bot import Action, describe_user, extract_message, handle_update
from formbot.forms import (
    Form,
    Question,
    Session,
    _v_choice,
    _v_date,
    _v_email,
    _v_number,
    _v_phone,
    match_choice,
    parse_date,
    advance,
    build_form,
    escape_html,
    format_submission,
    start,
)
from formbot.storage import CsvSubmissionStore, SessionStore

# Validators receive the question they belong to; the simple ones ignore it.
ANY_Q = Question(key="probe", text="probe")

FAILURES = []


def check(name, condition, extra=""):
    print(("PASS  " if condition else "FAIL  ") + name
          + ("" if condition else f"   <-- {extra}"))
    if not condition:
        FAILURES.append(name)


def make_form():
    return Form(
        questions=[
            Question(key="name", text="Как вас зовут?"),
            Question(key="phone", text="Ваш телефон?", validator="phone"),
            Question(key="comment", text="Комментарий?", optional=True),
        ],
        intro="Здравствуйте!",
        done="Заявка принята.",
    )


def update(text, chat_id=1, user_id=99, username="tester"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "text": text,
            "chat": {"id": chat_id},
            "from": {"id": user_id, "username": username},
        },
    }


# ---------------------------------------------------------------- validators

def test_phone_validator():
    for good in ["+7 999 123-45-67", "89991234567", "+1 (555) 010-9999"]:
        check(f"phone: accepts {good}", _v_phone(good, ANY_Q)[0])
    for bad in ["не скажу", "123", ""]:
        check(f"phone: rejects {bad!r}", not _v_phone(bad, ANY_Q)[0])


def test_email_validator():
    for good in ["a@b.co", "name.surname@example.com"]:
        check(f"email: accepts {good}", _v_email(good, ANY_Q)[0])
    for bad in ["a@b", "no-at-sign.com", "a b@c.com", ""]:
        check(f"email: rejects {bad!r}", not _v_email(bad, ANY_Q)[0])


def test_number_validator():
    check("number: accepts int", _v_number("1500", ANY_Q)[0])
    check("number: accepts decimal comma", _v_number("1,5", ANY_Q)[0])
    check("number: accepts spaced", _v_number("1 500", ANY_Q)[0])
    check("number: rejects words", not _v_number("много", ANY_Q)[0])


def choice_question(**kwargs):
    return Question(
        key="service", text="Какая услуга?", validator="choice",
        options=("Ремонт", "Доставка", "Консультация"), **kwargs)


def test_choice_validator():
    q = choice_question()

    check("choice: accepts exact option", _v_choice("Доставка", q)[0])
    check("choice: accepts different casing", _v_choice("дОсТаВкА", q)[0])
    check("choice: accepts surrounding whitespace", _v_choice("  Ремонт  ", q)[0])
    check("choice: accepts the option number", _v_choice("2", q)[0])
    check("choice: rejects a number out of range", not _v_choice("4", q)[0])
    check("choice: rejects zero", not _v_choice("0", q)[0])
    check("choice: rejects an unlisted answer", not _v_choice("что-нибудь", q)[0])
    check("choice: rejects empty", not _v_choice("", q)[0])
    check("choice: hint lists the options",
          "Ремонт" in _v_choice("нет", q)[1], _v_choice("нет", q)[1])


def test_choice_normalizes_to_the_configured_spelling():
    q = choice_question()
    check("choice: casing normalized to config", q.normalize("дОсТаВкА") == "Доставка")
    check("choice: number normalized to the option", q.normalize("3") == "Консультация")
    check("choice: whitespace stripped", q.normalize(" Ремонт ") == "Ремонт")


def test_choice_prompt_lists_numbered_options():
    prompt = choice_question().prompt
    check("choice: prompt keeps the question text", "Какая услуга?" in prompt, prompt)
    check("choice: prompt numbers every option",
          "1. Ремонт" in prompt and "2. Доставка" in prompt
          and "3. Консультация" in prompt, prompt)


def test_non_choice_prompt_is_unchanged():
    q = Question(key="name", text="Как вас зовут?")
    check("prompt: plain question is left alone", q.prompt == "Как вас зовут?")


def test_optional_choice_can_be_skipped():
    q = choice_question(optional=True)
    check("choice: optional question accepts a skip", q.validate("-")[0])
    check("choice: skip is recorded verbatim", q.normalize("-") == "-")


def test_match_choice_directly():
    options = ("Да", "Нет")
    check("match: returns the canonical option", match_choice("да", options) == "Да")
    check("match: resolves by index", match_choice("2", options) == "Нет")
    check("match: returns None when unmatched", match_choice("может", options) is None)


def test_date_validator():
    q = Question(key="when", text="Когда?", validator="date")

    for good in ["25.12.2026", "25/12/2026", "2026-12-25"]:
        check(f"date: accepts {good}", _v_date(good, q)[0])
    for bad in ["завтра", "32.12.2026", "30.02.2026", "12-25", ""]:
        check(f"date: rejects {bad!r}", not _v_date(bad, q)[0])


def test_date_normalizes_to_iso():
    q = Question(key="when", text="Когда?", validator="date")
    check("date: dotted form becomes ISO", q.normalize("25.12.2026") == "2026-12-25")
    check("date: slashed form becomes ISO", q.normalize("25/12/2026") == "2026-12-25")
    check("date: ISO stays ISO", q.normalize("2026-12-25") == "2026-12-25")


def test_parse_date_rejects_impossible_calendar_dates():
    check("date: rejects 31 February", parse_date("31.02.2026") is None)
    check("date: accepts a leap day", parse_date("29.02.2024") is not None)
    check("date: rejects a leap day in a common year", parse_date("29.02.2026") is None)


def test_build_form_validates_choice_options():
    def expect_error(name, question, fragment):
        try:
            build_form({"questions": [question]})
            check(name, False, "no ValueError raised")
        except ValueError as exc:
            check(name, fragment in str(exc), str(exc))

    expect_error(
        "config: choice without options is rejected",
        {"key": "s", "text": "?", "validator": "choice"}, "at least two")
    expect_error(
        "config: choice with one option is rejected",
        {"key": "s", "text": "?", "validator": "choice", "options": ["Да"]},
        "at least two")
    expect_error(
        "config: duplicate options are rejected",
        {"key": "s", "text": "?", "validator": "choice",
         "options": ["Да", "да"]}, "unique")
    expect_error(
        "config: empty option text is rejected",
        {"key": "s", "text": "?", "validator": "choice",
         "options": ["Да", "  "]}, "empty")
    expect_error(
        "config: options on a non-choice question are rejected",
        {"key": "s", "text": "?", "validator": "phone",
         "options": ["Да", "Нет"]}, "only applies")
    expect_error(
        "config: non-list options are rejected",
        {"key": "s", "text": "?", "validator": "choice", "options": "Да,Нет"},
        "must be a list")


def test_build_form_accepts_a_valid_choice():
    form = build_form({"questions": [
        {"key": "s", "text": "?", "validator": "choice",
         "options": ["Да", "Нет"]}]})
    check("config: valid choice builds", form.questions[0].options == ("Да", "Нет"),
          form.questions[0].options)


def test_choice_flows_end_to_end():
    form = build_form({"questions": [
        {"key": "service", "text": "Какая услуга?", "validator": "choice",
         "options": ["Ремонт", "Доставка"]},
        {"key": "when", "text": "Когда?", "validator": "date"},
    ]})
    session, reply = start(form)
    check("flow: options shown on the first prompt", "1. Ремонт" in reply.text,
          reply.text)

    session, bad = advance(form, session, "не знаю")
    check("flow: invalid choice re-asks with the options",
          "1. Ремонт" in bad.text and not bad.finished, bad.text)

    session, ok = advance(form, session, "2")
    check("flow: numeric choice advances", "Когда?" in ok.text, ok.text)

    session, done = advance(form, session, "25.12.2026")
    check("flow: choice and date land normalized",
          done.answers == {"service": "Доставка", "when": "2026-12-25"},
          done.answers)


# ---------------------------------------------------------------- flow

def test_start_asks_first_question():
    form = make_form()
    session, reply = start(form)
    check("start: index at zero", session.index == 0)
    check("start: intro included", "Здравствуйте!" in reply.text)
    check("start: first question asked", "Как вас зовут?" in reply.text)
    check("start: not finished", not reply.finished)


def test_full_happy_path():
    form = make_form()
    session, _ = start(form)

    session, r1 = advance(form, session, "Иван")
    check("flow: moves to question 2", "телефон" in r1.text.lower(), r1.text)

    session, r2 = advance(form, session, "+7 999 123-45-67")
    check("flow: moves to question 3", "Комментарий" in r2.text, r2.text)

    session, r3 = advance(form, session, "нужен звонок утром")
    check("flow: finishes", r3.finished)
    check("flow: all answers captured",
          r3.answers == {"name": "Иван", "phone": "+7 999 123-45-67",
                         "comment": "нужен звонок утром"}, r3.answers)


def test_invalid_answer_repeats_question():
    form = make_form()
    session, _ = start(form)
    session, _ = advance(form, session, "Иван")

    before = session.index
    session, reply = advance(form, session, "мой телефон секрет")
    check("validation: index does not advance", session.index == before)
    check("validation: hint given", "телефон" in reply.text.lower(), reply.text)
    check("validation: question repeated", "Ваш телефон?" in reply.text)
    check("validation: bad answer not stored", "phone" not in session.answers)

    session, reply = advance(form, session, "89991234567")
    check("validation: recovers after correction", session.index == before + 1)


def test_optional_question_can_be_skipped():
    form = make_form()
    session, _ = start(form)
    session, _ = advance(form, session, "Иван")
    session, _ = advance(form, session, "89991234567")
    session, reply = advance(form, session, "-")
    check("optional: skip accepted", reply.finished, reply.text)
    check("optional: skip stored as given", reply.answers["comment"] == "-")


def test_answers_are_trimmed():
    form = make_form()
    session, _ = start(form)
    session, _ = advance(form, session, "   Иван   ")
    check("flow: answer trimmed", session.answers["name"] == "Иван",
          repr(session.answers["name"]))


def test_empty_form_finishes_immediately():
    session, reply = start(Form(questions=[]))
    check("flow: empty form finishes at once", reply.finished)


# ---------------------------------------------------------------- config

def test_build_form_validation():
    good = build_form({"questions": [{"key": "a", "text": "A?"}]})
    check("config: builds form", len(good.questions) == 1)

    for bad, why in [
        ({"questions": []}, "empty list"),
        ({}, "missing questions"),
        ({"questions": [{"text": "no key"}]}, "missing key"),
        ({"questions": [{"key": "a"}]}, "missing text"),
        ({"questions": [{"key": "a", "text": "A?", "validator": "magic"}]},
         "unknown validator"),
        ({"questions": [{"key": "a", "text": "A?"},
                        {"key": "a", "text": "again?"}]}, "duplicate key"),
    ]:
        try:
            build_form(bad)
            check(f"config: rejects {why}", False)
        except ValueError:
            check(f"config: rejects {why}", True)


def test_duplicate_key_would_lose_data():
    """
    Regression guard: two questions sharing a key means the second answer
    silently overwrites the first, and the client loses a field.
    """
    try:
        build_form({"questions": [
            {"key": "phone", "text": "Рабочий телефон?"},
            {"key": "phone", "text": "Личный телефон?"},
        ]})
        check("config: duplicate keys rejected", False)
    except ValueError as exc:
        check("config: duplicate keys rejected", "duplicate" in str(exc).lower())


# ---------------------------------------------------------------- updates

def test_extract_message_ignores_non_text():
    check("update: ignores sticker",
          extract_message({"message": {"sticker": {}, "chat": {"id": 1}}}) is None)
    check("update: ignores empty update", extract_message({}) is None)
    check("update: accepts edited message",
          extract_message({"edited_message": {"text": "hi", "chat": {"id": 1}}})
          is not None)


def test_describe_user():
    _, who = describe_user({"from": {"id": 5, "username": "vasya"}})
    check("user: prefers username", who == "@vasya", who)
    _, who = describe_user({"from": {"id": 5, "first_name": "Иван",
                                     "last_name": "Петров"}})
    check("user: falls back to full name", who == "Иван Петров", who)
    _, who = describe_user({"from": {"id": 5}})
    check("user: falls back to id", who == "id5", who)


def test_handle_start_command():
    sessions = SessionStore()
    actions = handle_update(update("/start"), make_form(), sessions)
    check("handle: /start replies once", len(actions) == 1)
    check("handle: session created", sessions.get(1) is not None)


def test_handle_cancel_clears_session():
    sessions = SessionStore()
    handle_update(update("/start"), make_form(), sessions)
    actions = handle_update(update("/cancel"), make_form(), sessions)
    check("handle: /cancel clears session", sessions.get(1) is None)
    check("handle: /cancel confirms", len(actions) == 1)


def test_handle_text_before_start_begins_form():
    sessions = SessionStore()
    actions = handle_update(update("привет"), make_form(), sessions)
    check("handle: cold text starts the form", sessions.get(1) is not None)
    check("handle: first question asked",
          "Как вас зовут?" in actions[0].text, actions[0].text)


def test_handle_unknown_command():
    sessions = SessionStore()
    actions = handle_update(update("/wat"), make_form(), sessions)
    check("handle: unknown command nudges to /start",
          "/start" in actions[0].text, actions[0].text)


def test_handle_completion_produces_save_and_admin_actions():
    form = make_form()
    sessions = SessionStore()
    handle_update(update("/start"), form, sessions)
    handle_update(update("Иван"), form, sessions)
    handle_update(update("89991234567"), form, sessions)
    actions = handle_update(update("-"), form, sessions,
                            admin_chat_id="777")

    kinds = [a.kind for a in actions]
    check("handle: replies, saves and notifies", kinds
          == ["reply", "save", "notify_admin"], kinds)
    check("handle: session cleared after completion", sessions.get(1) is None)

    save = next(a for a in actions if a.kind == "save")
    check("handle: answers passed to save",
          save.payload["answers"]["name"] == "Иван", save.payload)
    check("handle: meta includes username",
          save.payload["meta"]["username"] == "@tester", save.payload)

    admin = next(a for a in actions if a.kind == "notify_admin")
    check("handle: admin message goes to admin chat", admin.chat_id == "777")


def test_no_admin_means_no_admin_action():
    form = make_form()
    sessions = SessionStore()
    handle_update(update("/start"), form, sessions)
    handle_update(update("Иван"), form, sessions)
    handle_update(update("89991234567"), form, sessions)
    actions = handle_update(update("-"), form, sessions)
    check("handle: no admin configured means no notify action",
          "notify_admin" not in [a.kind for a in actions])


def test_two_users_do_not_share_state():
    form = make_form()
    sessions = SessionStore()
    handle_update(update("/start", chat_id=1), form, sessions)
    handle_update(update("/start", chat_id=2), form, sessions)
    handle_update(update("Иван", chat_id=1), form, sessions)

    check("isolation: user 1 advanced", sessions.get(1).index == 1)
    check("isolation: user 2 unaffected", sessions.get(2).index == 0)


# ---------------------------------------------------------------- storage

def test_csv_submission_store():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "subs.csv")
        store = CsvSubmissionStore(path, ["name", "phone"])
        check("store: starts empty", store.count() == 0)

        store.add({"name": "Иван", "phone": "89991234567"},
                  {"user_id": "5", "username": "@vasya"})
        store.add({"name": "Пётр", "phone": "89990000000"},
                  {"user_id": "6", "username": "@petr"})
        check("store: counts submissions", store.count() == 2, store.count())

        content = open(path, encoding="utf-8-sig").read()
        check("store: header written",
              content.startswith("timestamp,user_id,username,name,phone"),
              content[:60])
        check("store: cyrillic preserved", "Иван" in content)
        check("store: BOM for Excel", open(path, "rb").read(3) == b"\xef\xbb\xbf")


def test_csv_store_appends_across_restarts():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "subs.csv")
        CsvSubmissionStore(path, ["name"]).add({"name": "A"}, {})
        # Simulate a restart: a fresh store on the same file.
        second = CsvSubmissionStore(path, ["name"])
        second.add({"name": "B"}, {})
        check("store: survives restart without losing rows",
              second.count() == 2, second.count())


def test_session_store_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sessions.json")
        store = SessionStore(path)
        store.set(42, Session(index=2, answers={"name": "Иван"}))
        store.save()

        reopened = SessionStore(path)
        restored = reopened.get(42)
        check("sessions: survive restart", restored is not None)
        check("sessions: index preserved", restored.index == 2, restored)
        check("sessions: answers preserved",
              restored.answers == {"name": "Иван"}, restored)


def test_session_store_handles_corrupt_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sessions.json")
        with open(path, "w") as fh:
            fh.write("{ broken")
        store = SessionStore(path)
        check("sessions: corrupt file does not crash", store.active() == 0)


# ---------------------------------------------------------------- formatting

def test_format_submission():
    form = make_form()
    text = format_submission(
        {"name": "Иван", "phone": "89991234567"}, form, who="@vasya"
    )
    check("format: includes who", "@vasya" in text)
    check("format: includes answers", "Иван" in text)
    check("format: missing answer shown as dash", "—" in text, text)
    check("format: follows question order",
          text.index("Как вас зовут?") < text.index("Ваш телефон?"))


def test_html_injection_is_escaped():
    """
    Regression: answers go into a parse_mode=HTML message. A user typing
    "<" made Telegram reject the whole notification, so the admin silently
    never received that lead.
    """
    form = make_form()
    text = format_submission(
        {"name": "<script>alert(1)</script>", "phone": "1 & 2"}, form, "@u"
    )
    check("security: script tag escaped", "<script>" not in text, text[:80])
    check("security: escaped form present", "&lt;script&gt;" in text)
    check("security: ampersand escaped", "&amp;" in text)
    check("security: bot's own markup still intact",
          "<b>Новая заявка</b>" in text)


def test_long_answer_is_truncated():
    """Telegram rejects messages over 4096 chars — a rejected message is a lost lead."""
    form = make_form()
    text = format_submission({"name": "x" * 10000}, form, "@u")
    check("limits: message stays under Telegram's cap",
          len(text) < 4096, len(text))
    check("limits: truncation is visible", "обрезано" in text)


def test_escape_preserves_cyrillic():
    check("security: escaping does not mangle Cyrillic",
          escape_html("Иван Петров") == "Иван Петров")


# ---------------------------------------------------------------- run

if __name__ == "__main__":
    for fn in [
        test_phone_validator,
        test_email_validator,
        test_number_validator,
        test_choice_validator,
        test_choice_normalizes_to_the_configured_spelling,
        test_choice_prompt_lists_numbered_options,
        test_non_choice_prompt_is_unchanged,
        test_optional_choice_can_be_skipped,
        test_match_choice_directly,
        test_date_validator,
        test_date_normalizes_to_iso,
        test_parse_date_rejects_impossible_calendar_dates,
        test_build_form_validates_choice_options,
        test_build_form_accepts_a_valid_choice,
        test_choice_flows_end_to_end,
        test_start_asks_first_question,
        test_full_happy_path,
        test_invalid_answer_repeats_question,
        test_optional_question_can_be_skipped,
        test_answers_are_trimmed,
        test_empty_form_finishes_immediately,
        test_build_form_validation,
        test_duplicate_key_would_lose_data,
        test_extract_message_ignores_non_text,
        test_describe_user,
        test_handle_start_command,
        test_handle_cancel_clears_session,
        test_handle_text_before_start_begins_form,
        test_handle_unknown_command,
        test_handle_completion_produces_save_and_admin_actions,
        test_no_admin_means_no_admin_action,
        test_two_users_do_not_share_state,
        test_csv_submission_store,
        test_csv_store_appends_across_restarts,
        test_session_store_persistence,
        test_session_store_handles_corrupt_file,
        test_format_submission,
        test_html_injection_is_escaped,
        test_long_answer_is_truncated,
        test_escape_preserves_cyrillic,
    ]:
        fn()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print("all tests passed")
