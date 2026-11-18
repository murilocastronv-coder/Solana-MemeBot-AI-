import pathlib
import tempfile
import unittest
from unittest import mock

import owner_notify as on
import user_trade as ut


class OwnerNotifyTest(unittest.TestCase):
    def setUp(self):
        on._MEM.clear()
        self.sent = []
        on.setup(lambda chat, text, *a: self.sent.append((chat, text)), lambda: 100)
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.p = [mock.patch.object(on, "_save"), mock.patch.object(on, "ENABLED", True), mock.patch.object(ut.user_wallets, "_base_dir", return_value=self.dir)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_new_person_told_once_and_counted(self):
        self.assertTrue(on.visit(5, {"first_name": "Ana", "username": "ana1"}, now=1000))
        self.assertFalse(on.visit(5, {"first_name": "Ana"}, now=2000))
        on.visit(6, {"first_name": "Beto"}, now=3000)
        self.assertEqual([c for c, _ in self.sent], [100, 100])
        self.assertIn("Ana (@ana1)", self.sent[0][1])
        self.assertIn("2 no total", on.summary(now=3000))
        self.assertIn("hoje 2", on.summary(now=3000))

    def test_deposit_detected_but_not_after_bot_trade(self):
        bal = {"x": 100_000_000}
        with mock.patch.object(ut.user_wallets, "get_address", return_value="ADDR"):
            on.check_deposits(chats=["5"], balance=lambda a: bal["x"])          # first look: just remembers
            bal["x"] = 600_000_000
            found = on.check_deposits(chats=["5"], balance=lambda a: bal["x"])
            self.assertEqual(found, [("5", 500_000_000)])
            self.assertIn("DEPÓSITO", self.sent[-1][1])
            self.assertIn("0.5000 SOL", self.sent[-1][1])
            ut.update("5", lambda d: d.__setitem__("busy_until", 9e12))       # a sale being confirmed
            bal["x"] = 900_000_000
            self.assertEqual(on.check_deposits(chats=["5"], balance=lambda a: bal["x"]), [])


if __name__ == "__main__":
    unittest.main()


class NeverHeldBackTest(unittest.TestCase):
    def test_owner_notices_always_sent(self):
        import telegram_bot as tb
        for text in ("📥 DEPÓSITO: Ana · id 5 depositou 0.5000 SOL", "👤 NOVA PESSOA NO BOT: Ana · id 5",
                     "🟢 COMPRA DE USUÁRIO: Ana", "🔴 VENDA DE USUÁRIO: Ana", "💸 SAQUE DE USUÁRIO: Ana"):
            self.assertTrue(tb.should_send(text, now=1000, chat=1))
            self.assertTrue(tb.should_send(text, now=1001, chat=1))
