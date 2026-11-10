import os
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
import telegram_bot as tb
import user_wallets as uw

STRONG = "Forte#Senha2026"


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

    def test_strong_password_rules(self):
        for weak in ("curta1!A", "semnumero!Aaaa", "SEMMINUSCULA1!", "semsimbolo1Aaa", "aaaaaaaaaaaA1!"):
            self.assertIsNotNone(uw.password_problem(weak), weak)
        self.assertIsNone(uw.password_problem(STRONG, STRONG))
        self.assertIsNotNone(uw.password_problem(STRONG, STRONG + "x"))

    def test_first_export_creates_password_then_requires_it(self):
        address, _ = uw.create_or_get(301)
        self.assertEqual(uw.export_with_password(301, "fraca", "fraca", create=True)[0], "error")
        self.assertEqual(uw.export_with_password(301, STRONG, STRONG, create=True)[0], "created")   # no key yet
        status, addr, key = uw.export_with_password(301, STRONG)                                     # log in
        self.assertEqual((status, addr), ("ok", address))
        from solders.keypair import Keypair
        self.assertEqual(str(Keypair.from_base58_string(key).pubkey()), address)
        self.assertEqual(uw.export_with_password(301, "Errada#Senha99")[0], "error")
        self.assertEqual(uw.export_with_password(301, STRONG)[:2], ("ok", address))
        self.assertEqual(uw.export_with_password(301, STRONG, STRONG, create=True)[0], "error")   # can't overwrite
        self.assertNotIn(STRONG, uw._passwords_file().read_text())                              # only a hash

    def test_too_many_wrong_passwords_lock_export(self):
        uw.create_or_get(401)
        uw.export_with_password(401, STRONG, STRONG, create=True)
        for _ in range(uw.MAX_FAILS):
            uw.export_with_password(401, "Errada#Senha99")
        self.assertEqual(uw.export_with_password(401, STRONG)[0], "error")

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

    def test_export_page_create_then_unlock(self):
        sent = []
        with patch.object(tb, "request_api"), patch.object(tb, "CHAT", "100"), \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append(kb)):
            tb.handle({"callback_query": {"id": "1", "data": "wallet_export", "message": {"chat": {"id": 777, "type": "private"}}}})
        url = sent[-1][0][0]["url"]
        token = re.search(r"token=(.+)$", url).group(1)
        client = tb.app.test_client()
        page = client.get("/exportar?token=" + token).get_data(as_text=True)
        self.assertIn("Crie sua senha forte", page)
        bad = client.post("/exportar?token=" + token, data={"mode": "create", "password": "fraca", "confirm": "fraca"})
        self.assertEqual(bad.status_code, 400)
        made = client.post("/exportar?token=" + token, data={"mode": "create", "password": STRONG, "confirm": STRONG})
        self.assertIn("Senha criada", made.get_data(as_text=True))
        self.assertNotIn("Chave privada", made.get_data(as_text=True))               # step 2: log in first
        ok = client.post("/exportar?token=" + token, data={"mode": "unlock", "password": STRONG})
        self.assertIn("Chave privada", ok.get_data(as_text=True))
        self.assertEqual(client.get("/exportar?token=" + token).status_code, 403)   # one use only
        with patch.object(tb, "request_api"), patch.object(tb, "CHAT", "100"), \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append(kb)):
            tb.handle({"callback_query": {"id": "1", "data": "wallet_export", "message": {"chat": {"id": 777, "type": "private"}}}})
        token = re.search(r"token=(.+)$", sent[-1][0][0]["url"]).group(1)
        self.assertIn("senha de exportação", client.get("/exportar?token=" + token).get_data(as_text=True))
        ok = client.post("/exportar?token=" + token, data={"mode": "unlock", "password": STRONG})
        self.assertIn("Chave privada", ok.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
