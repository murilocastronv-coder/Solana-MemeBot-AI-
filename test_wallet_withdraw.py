import os
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import wallet_withdraw as ww

BOT = str(Keypair().pubkey())
DEST = str(Keypair().pubkey())


class WithdrawLogicTests(unittest.TestCase):
    def test_address_validation(self):
        with patch.object(ww, "get_address", return_value=BOT):
            self.assertEqual(ww.valid_address(" " + DEST + " "), DEST)
            self.assertIsNone(ww.valid_address(BOT))
            self.assertIsNone(ww.valid_address("abc"))

    def test_amounts(self):
        bal = 2 * ww.LAMPORTS
        self.assertEqual(ww.amount_lamports("0,5", bal), ww.LAMPORTS // 2)
        self.assertEqual(ww.amount_lamports("tudo", bal), bal - ww.KEEP_ON_ALL)
        for bad in ("2", "x", "0"):
            with self.assertRaises(ww.WithdrawError):
                ww.amount_lamports(bad, bal)


class WithdrawFlowTests(unittest.TestCase):
    def talk(self, tb, out, text=None, data=None):
        upd = ({"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": 1, "type": "private"}}}} if data else
               {"message": {"chat": {"id": 1, "type": "private"}, "text": text}})
        tb.handle(upd)
        return out[-1]

    def test_full_flow_needs_confirmation(self):
        import telegram_bot as tb
        out = []
        with patch.object(tb, "CHAT", "1"), patch.object(tb, "send", side_effect=lambda chat, text, kb=None: out.append(text)), \
                patch.object(tb, "request_api", return_value={}), patch.object(tb, "run_async", side_effect=lambda fn: fn()), \
                patch.object(ww, "get_address", return_value=BOT), \
                patch.object(ww, "balance_lamports", return_value=3 * ww.LAMPORTS), \
                patch.object(ww, "send_sol", return_value="SIG") as sent:
            self.assertIn("ENDEREÇO", self.talk(tb, out, data="wd_start"))
            self.assertIn("inválido", self.talk(tb, out, text="nada"))
            self.assertIn("Quanto retirar", self.talk(tb, out, text=DEST))
            self.assertIn("Confirma", self.talk(tb, out, text="1.5"))
            sent.assert_not_called()
            self.assertIn("SAQUE ENVIADO", self.talk(tb, out, data="wd_ok"))
            sent.assert_called_once_with(DEST, int(1.5 * ww.LAMPORTS))
            self.talk(tb, out, data="wd_start")
            self.assertIn("cancelado", self.talk(tb, out, text="/cancelar"))
            self.assertNotIn(1, tb.WITHDRAW)

    def test_menu_has_deposit_and_withdraw(self):
        import telegram_bot as tb
        with patch.object(tb, "OWNER_MENU", "btc"):
            data = {b["callback_data"] for row in tb.keyboard() for b in row}
        self.assertTrue({"wallet_deposit", "wd_start", "wallet_balance"} <= data)


if __name__ == "__main__":
    unittest.main()
