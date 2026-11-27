"""The Telegram "Menu" button and polling setup."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
import telegram_bot as tb


class TelegramMenuTests(unittest.TestCase):
    def test_setup_registers_commands_and_removes_webhook(self):
        calls = []
        with patch.object(tb, "TOKEN", "x"), patch.object(tb, "request_api",
                                                          side_effect=lambda m, **p: calls.append((m, p)) or {"ok": True}):
            self.assertTrue(tb.setup_telegram_menu())
        methods = [m for m, _ in calls]
        self.assertEqual(methods, ["deleteWebhook", "setMyCommands", "setChatMenuButton"])
        cmds = [c["command"] for c in calls[1][1]["commands"]]
        self.assertIn("start", cmds)
        self.assertIn("menu", cmds)
        for c in calls[1][1]["commands"]:
            self.assertRegex(c["command"], r"^[a-z0-9_]{1,32}$")
            self.assertTrue(0 < len(c["description"]) <= 256)

    def test_setup_survives_telegram_errors(self):
        def boom(method, **params):
            raise RuntimeError("Telegram API returned HTTP 500")
        with patch.object(tb, "TOKEN", "x"), patch.object(tb, "request_api", side_effect=boom):
            self.assertFalse(tb.setup_telegram_menu())

    def test_no_token_does_nothing(self):
        with patch.object(tb, "TOKEN", ""), patch.object(tb, "request_api") as api:
            self.assertFalse(tb.setup_telegram_menu())
            api.assert_not_called()

    def test_every_menu_command_is_handled(self):
        handled = {"start", "menu", "id", "posicoes", "ordens", "btc", "resultados", "saldo", "depositar", "sacar",
                   "config", "ajuda"}
        self.assertEqual({c for c, _ in tb.MENU_COMMANDS}, handled)


if __name__ == "__main__":
    unittest.main()
