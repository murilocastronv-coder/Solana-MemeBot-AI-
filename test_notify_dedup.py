import os
import unittest
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
import telegram_bot as tb


class NotifyDedupTests(unittest.TestCase):
    def setUp(self):
        tb.SENT_AT.clear()
        tb.CTX.interactive = False

    def test_same_notice_once_every_two_hours(self):
        t = 1_000_000.0
        self.assertTrue(tb.should_send("⏳ 2 operação(ões) pendente(s).", t))
        self.assertFalse(tb.should_send("⏳ 3 operação(ões) pendente(s).", t + 60))      # only numbers changed
        self.assertFalse(tb.should_send("⏳ 2 operação(ões) pendente(s).", t + 7199))
        self.assertTrue(tb.should_send("⏳ 2 operação(ões) pendente(s).", t + 7201))

    def test_buys_and_sells_always_sent(self):
        for text in ("🟢 COPY COMPROU BONK\nabc", "💰 LUCRO — VENDEU BONK", "👥 SIM copiou compra de AbCd…: mint · US$ 32.00",
                     "👥 SIM 🔻 vendeu BONK (a carteira vendeu): -1.00 USD", "🔴 SHORT BTC 5x"):
            for _ in range(3):
                self.assertTrue(tb.should_send(text, 5.0), text)

    def test_errors_are_not_trades(self):
        self.assertFalse(tb.is_trade_message("⛔ BTC breakout REAL: não consegui FECHAR (x)"))
        self.assertFalse(tb.is_trade_message("🗑️ Parei de copiar 1 carteira(s) (sem lucro nas cópias)"))

    def test_answers_to_owner_never_held_back(self):
        tb.CTX.interactive = True
        self.assertTrue(tb.should_send("📊 status", 1.0))
        self.assertTrue(tb.should_send("📊 status", 2.0))

    def test_send_skips_repeat_without_api_call(self):
        with patch.object(tb, "request_api", return_value={"ok": True}) as api:
            tb.send(1, "🔄 aviso repetido")
            tb.send(1, "🔄 aviso repetido")
        self.assertEqual(api.call_count, 1)


class FillWalletsTests(unittest.TestCase):
    def test_more_audits_while_list_has_room(self):
        import copy_trader as c
        with patch.object(c, "free_auto_slots", return_value=10):
            self.assertEqual(c.preaudit_budget(), c.PREAUDIT_FILL_PER_CYCLE)
        with patch.object(c, "free_auto_slots", return_value=0):
            self.assertEqual(c.preaudit_budget(), c.PREAUDIT_PER_CYCLE)
        self.assertEqual(c.MAX_AUTO_WALLETS, 20)


if __name__ == "__main__":
    unittest.main()
