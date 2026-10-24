import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
#!/usr/bin/env python3
import unittest
from unittest import mock

import telegram_bot


class TelegramBotTests(unittest.TestCase):
    def setUp(self):
        telegram_bot.CONVERSATIONS.clear()

    def test_followup_keyboard_uses_safe_callback_queries(self):
        keyboard = telegram_bot.followup_keyboard({
            "experience": {"followups": ["Compare port and starboard", "x" * 70]}
        })
        self.assertEqual("q:Compare port and starboard", keyboard["inline_keyboard"][0][0]["callback_data"])
        self.assertEqual(1, len(keyboard["inline_keyboard"]))

    def test_shortcut_is_sent_as_natural_language_question(self):
        calls = []

        def fake_api(token, method, payload=None, timeout=35):
            if method == "sendMessage":
                return {"result": {"message_id": 7}}
            calls.append((method, payload))
            return {"ok": True}

        with mock.patch.object(telegram_bot, "telegram_api", side_effect=fake_api), \
             mock.patch.object(telegram_bot, "chat_with_boat", return_value={"answer": "All clear", "experience": {}}) as chat, \
             mock.patch.object(telegram_bot, "save_conversations"):
            telegram_bot.handle_message("token", {"TELEGRAM_ALLOWED_CHAT_IDS": "123"}, {
                "chat": {"id": 123}, "text": "/brief"
            })
        self.assertEqual("Give me a current boat status briefing", chat.call_args.args[1])
        self.assertIn(("editMessageText", mock.ANY), calls)

    def test_callback_runs_followup_in_same_chat(self):
        callback = {"id": "cb1", "data": "q:Show the last trip", "message": {"chat": {"id": 123}, "message_id": 9}}
        with mock.patch.object(telegram_bot, "telegram_api", return_value={"ok": True}) as api, \
             mock.patch.object(telegram_bot, "handle_message") as handle:
            telegram_bot.handle_callback_query("token", {}, callback)
        api.assert_called_once_with("token", "answerCallbackQuery", {"callback_query_id": "cb1"})
        handle.assert_called_once_with("token", {}, callback["message"], override_text="Show the last trip")


if __name__ == "__main__":
    unittest.main()
