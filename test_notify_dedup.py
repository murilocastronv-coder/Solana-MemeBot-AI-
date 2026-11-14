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
                     "👥 SIM 🔻 vendeu BONK (a carteira vendeu): -1.00 USD", "🔻 PREJUÍZO — VENDEU WIF"):
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


class MenuTests(unittest.TestCase):
    def test_menu_has_only_copy_trade_and_bot_wallet(self):
        data = {b["callback_data"] for row in tb.keyboard() for b in row}
        self.assertEqual(data, {"rk_on", "rk_off", "rk_status", "rks_on", "rks_off", "unstick", "wallet_deposit", "wallet_balance",
                                "wallet_withdraw"})

    def test_removed_buttons_answer_removed(self):
        tb.CTX.interactive = True
        with patch.object(tb, "authorized", return_value=True), patch.object(tb, "request_api"), \
                patch.object(tb, "send") as send:
            for action in ("brk_sim", "strat_real", "clone_help", "/clonar"):
                tb.handle({"message": {"chat": {"id": 1, "type": "private"}, "text": action}})
        self.assertTrue(all("removido" in call.args[1] for call in send.call_args_list))


class StartRealTests(unittest.TestCase):
    def test_turning_real_on_while_an_order_is_checked_waits_instead_of_failing(self):
        tb.CTX.interactive = True
        sent = []
        with patch.object(tb, "authorized", return_value=True), patch.object(tb, "request_api"), \
                patch.object(tb.live_engine, "start", side_effect=RuntimeError("Unreconciled transaction: trading paused")), \
                patch.object(tb.trade_state, "pending_orders", return_value={"o": {}}), \
                patch.object(tb, "run_async") as later, \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append(t)), \
                patch("copy_trader.set_mode") as mode:
            tb.handle({"callback_query": {"id": "1", "data": "copy_real", "message": {"chat": {"id": 1, "type": "private"}}}})
        mode.assert_called_once_with(True, True)
        later.assert_called_once()                                   # starts by itself once the order is checked
        self.assertIn("volta a comprar sozinho", sent[-1])
        self.assertFalse(any("Unreconciled" in t for t in sent))


if __name__ == "__main__":
    unittest.main()
