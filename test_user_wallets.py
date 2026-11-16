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


    def _cb(self, chat, data):
        tb.handle({"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": chat, "type": "private"}}}})

    def test_owner_can_export_private_key_after_confirming(self):
        sent, api = [], []
        owner_addr, _ = uw.create_or_get(100)
        with patch.object(tb, "request_api", side_effect=lambda m, **k: api.append((m, k)) or {"result": {"message_id": 9}}), \
                patch.object(tb, "CHAT", "100"), patch.object(tb, "EXPORT_DELETE_S", 0), \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append((t, kb))):
            self._cb(100, "wallet_export")
            self.assertIn("Sim, mostrar a chave", str(sent[-1][1]))
            self.assertFalse([k for m, k in api if m == "sendMessage"])     # nothing before confirming
            self._cb(100, "wallet_export_ok")
            import time; time.sleep(0.2)
        msgs = [k for m, k in api if m == "sendMessage"]
        self.assertEqual(len(msgs), 1)
        from solders.keypair import Keypair
        key = re.search(r"\n\n(\w{80,90})\n\n", msgs[0]["text"]).group(1)
        self.assertEqual(str(Keypair.from_base58_string(key).pubkey()), owner_addr)
        self.assertTrue(msgs[0]["protect_content"])
        self.assertTrue(any(m == "deleteMessage" for m, _ in api))           # message wiped afterwards
        self.assertFalse(any(key in t for t, _ in sent))                     # never through send()/panel
        self.assertIn("wallet_export", {b.get("callback_data") for row in tb.keyboard() for b in row})

    def test_export_needs_confirmation_and_is_owner_only(self):
        sent, api = [], []
        uw.create_or_get(100); uw.create_or_get(777)
        with patch.object(tb, "request_api", side_effect=lambda m, **k: api.append((m, k)) or {}), \
                patch.object(tb, "CHAT", "100"), \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append((t, kb))):
            self._cb(100, "wallet_export_ok")                                # no prior request -> expired
            self._cb(777, "wallet_export")
            self._cb(777, "wallet_export_ok")
        self.assertFalse([k for m, k in api if m == "sendMessage"])
        self.assertIn("expirada", sent[0][0])
        self.assertIn("só para o dono", sent[1][0])
        self.assertNotIn("wallet_export", {b.get("callback_data") for row in tb.user_keyboard() for b in row})
        client = tb.app.test_client()
        self.assertEqual(client.get("/exportar?token=x").status_code, 410)  # no web export page


if __name__ == "__main__":
    unittest.main()
