"""Fault injection at the CSV/session/cursor commit boundary; no network."""
import os
import tempfile
import unittest
from unittest.mock import patch

from formbot.bot import Bot
from formbot.storage import atomic_text


class RecoveryTests(unittest.TestCase):
    def test_restart_finishes_partial_commit_without_duplicate_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"bot_token": "test", "questions": [{"key": "name", "text": "Name?"}],
                      "sessions_path": os.path.join(tmp, "sessions.json"),
                      "offset_path": os.path.join(tmp, "offset.json"),
                      "submissions_path": os.path.join(tmp, "submissions.csv")}
            bot = Bot(config)
            def update(i, text):
                return {"update_id": i, "message": {"chat": {"id": 1}, "text": text}}
            with patch.object(bot.api, "send_message", return_value=True):
                bot.process_update(update(1, "/start"))
            def fail_after_csv(path, *args, **kwargs):
                if path == config["sessions_path"]:
                    raise OSError("disk full after CSV replace")
                return atomic_text(path, *args, **kwargs)
            with patch("formbot.bot.atomic_text", side_effect=fail_after_csv):
                with self.assertRaises(OSError):
                    bot.process_update(update(2, "Alice"))
            self.assertTrue(os.path.exists(bot.journal_path))
            reopened = Bot(config)
            self.assertEqual(reopened.submissions.count(), 1)
            self.assertEqual(reopened.offset.value, 3)
            self.assertIsNone(reopened.sessions.get(1))
            with patch.object(reopened.api, "send_message") as send:
                reopened.process_update(update(2, "Alice"))
                send.assert_not_called()
            self.assertEqual(reopened.submissions.count(), 1)


if __name__ == "__main__":
    unittest.main()
