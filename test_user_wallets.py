import os
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
import telegram_bot as tb
import user_wallets as uw



class UserWalletTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "wallet.json"), "TELEGRAM_CHAT_ID": "100",
                                    "BOT_WALLET_ENCRYPTION_PASSPHRASE": "x" * 32})
        p.start()
        self.addCleanup(p.stop)
        uw._FAILS.clear()

    def test_each_telegram_gets_its_own_unique_wallet(self):
        a, created_a = uw.create_or_get(201)
        b, created_b = uw.create_or_get(202)
        self.assertTrue(created_a and created_b)
        self.assertNotEqual(a, b)
        self.assertEqual(uw.create_or_get(201), (a, False))          # same account -> same wallet
        owner, _ = uw.create_or_get(100)                              # owner -> main bot wallet
        self.assertNotIn(owner, (a, b))
        self.assertEqual(uw.get_address(100), tb.get_address())

    def test_other_accounts_only_get_wallet_menu(self):
        sent = []
        with patch.object(tb, "request_api"), patch.object(tb, "CHAT", "100"), \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append((c, t, kb))):
            tb.handle({"message": {"chat": {"id": 555, "type": "private"}, "text": "/start"}})
            tb.handle({"callback_query": {"id": "1", "data": "copy_real", "message": {"chat": {"id": 555, "type": "private"}}}})
        self.assertIn("CARTEIRA CRIADA", sent[0][1])
        buttons = {b["callback_data"] for _, _, kb in sent for row in kb for b in row}
        self.assertFalse({"copy_real", "copy_sim", "copy_off"} & buttons)
        import copy_trader
        self.assertFalse(copy_trader.real_enabled())                  # a stranger can't switch trading on


    def test_private_key_export_is_disabled_for_everyone(self):
        sent = []
        with patch.object(tb, "request_api"), patch.object(tb, "CHAT", "100"), \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append((t, kb))):
            for chat in (100, 777):                                     # owner and another account
                tb.handle({"callback_query": {"id": "1", "data": "wallet_export",
                                              "message": {"chat": {"id": chat, "type": "private"}}}})
        self.assertEqual(sum("exportação de chave privada foi DESATIVADA" in t for t, _ in sent), 2)
        self.assertFalse(any("url" in b for _, kb in sent if kb for row in kb for b in row))   # no export link
        for kb in (tb.keyboard(), tb.user_keyboard()):
            self.assertNotIn("wallet_export", {b.get("callback_data") for row in kb for b in row})
        client = tb.app.test_client()
        self.assertEqual(client.get("/exportar?token=x").status_code, 410)
        self.assertEqual(client.post("/exportar?token=x", data={"password": "x"}).status_code, 410)
        self.assertEqual(client.get("/carteira").status_code, 404)        # key test page removed
        self.assertFalse(hasattr(uw, "export_with_password"))


if __name__ == "__main__":
    unittest.main()
