# FormBot

A Telegram bot that asks your questions, checks the answers, and drops every submission into a spreadsheet. You describe the form in JSON — no Python per client.

Built for the most common bot request there is: *"I need a bot that collects applications."*

---

## Quick start

```bash
pip install -r requirements.txt
cp config.example.json config.json
```

1. Message **@BotFather** → `/newbot` → copy the token into `bot_token`
2. Message **@userinfobot** → copy your id into `admin_chat_id`
3. Edit `questions`

```bash
python3 run.py
```

Submissions land in `submissions.csv`. You also get each one as a Telegram message.

---

## Config

```json
{
  "bot_token": "...",
  "admin_chat_id": "123456789",

  "intro": "Здравствуйте! Заполним заявку.",
  "done": "Спасибо! Мы свяжемся с вами.",

  "questions": [
    { "key": "name",  "text": "Как вас зовут?" },
    { "key": "phone", "text": "Телефон?", "validator": "phone" },
    { "key": "email", "text": "Email?", "validator": "email", "optional": true }
  ]
}
```

| question key | meaning |
|---|---|
| `key` | column name in the CSV — must be unique |
| `text` | what the bot asks |
| `validator` | `any` (default), `phone`, `email`, `number` |
| `optional` | user may answer `-`, `нет`, `пропустить`, `skip` |

### Validation

A bad answer does **not** advance the form. The bot explains and asks again:

```
> Ваш телефон?
< не скажу
> Не похоже на телефон. Пример: +7 999 123-45-67
> Ваш телефон?
```

That is the difference between a lead list and a file full of `asdf`.

### Commands

`/start` restart · `/cancel` abandon · `/help` list commands

Unknown commands get a nudge toward `/start` rather than silence.

---

## What you get

**CSV**, opened straight in Excel — with a BOM so Cyrillic is not mojibake:

```
timestamp,user_id,username,name,phone,email,budget,task
2026-08-02T01:22:33+00:00,777,@klient,Иван Петров,+7 999 123-45-67,-,50000,нужен парсер
```

**A Telegram message** per submission, if `admin_chat_id` is set.

Appending, not rewriting: a crash cannot lose earlier submissions, and you can open the file while the bot runs.

---

## Architecture

```
api.py       Telegram calls          returns plain Python, never raises
forms.py     the conversation        pure: (state, input) -> (state, reply)
storage.py   submissions & sessions  behind small interfaces
bot.py       wiring                  handle_update is pure; only Bot does I/O
```

**Adding anything is one function plus one registry line.**

A new validator:

```python
def _v_inn(value):
    digits = re.sub(r"\D", "", value)
    return (True, "") if len(digits) in (10, 12) else (False, "ИНН — 10 или 12 цифр")

VALIDATORS["inn"] = _v_inn   # usable in config immediately
```

`handle_update(update, form, sessions, admin_chat_id) -> [Action]` performs no I/O — it returns *what should happen*. That is why the entire bot is tested with dictionaries and no Telegram token.

Swapping the CSV for a database means one class implementing `add()` and `count()`.

---

## Tests

```bash
python3 tests/test_formbot.py
```

83 tests: validators against real-world input, the full conversation flow, command handling, per-user isolation, storage persistence, and the security fixes below.

### Bugs found by probing the first version

Each is now locked down by a regression test:

- **HTML injection lost leads silently.** Answers are sent to the admin with `parse_mode=HTML`. One person typing `<` made Telegram reject the entire notification — the admin never learned a submission existed. All user text is now escaped.
- **Long answers exceeded Telegram's 4096-character limit**, with the same silent-rejection result. Values are capped and the message is truncated visibly.
- **Duplicate question keys silently overwrote answers.** Two questions both keyed `phone` meant the client lost a field with no warning. Now rejected at config load.

---

## Running it continuously

```bash
nohup python3 run.py > formbot.log 2>&1 &
```

systemd, for a small VPS:

```ini
[Unit]
Description=FormBot
After=network.target

[Service]
WorkingDirectory=/path/to/formbot
ExecStart=/usr/bin/python3 /path/to/formbot/run.py
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Behaviour notes

- **Sessions survive a restart.** Someone halfway through the form is not stranded.
- **Each user is independent.** Two people filling the form at once never see each other's state.
- **Non-text messages are ignored** — stickers, photos and joins do not derail a form.
- **One malformed update cannot kill the bot.** It is logged and polling continues.
- Typing before `/start` begins the form rather than scolding the user.

## Limits

- Long polling, not webhooks. Simpler to run; fine up to a few messages per second.
- Text answers only. Photos and documents are not collected in this version.
- One form per bot. Several forms means several bots, or a menu layer on top.
