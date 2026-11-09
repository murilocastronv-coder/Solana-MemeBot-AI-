import os
import tempfile
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import copy_trader as c


def cand(pnl=10**9, holds=(400, 500, 600), trades=5):
    return {"wallet": str(Keypair().pubkey()), "closed_trades": trades, "realized_lamports": pnl,
            "hold_samples": list(holds)}


class RotationTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        c.EVENTS.clear()

    def fill(self):
        ws = [cand() for _ in range(c.MAX_AUTO_WALLETS)]
        for w in ws:
            self.assertTrue(c.auto_follow(w))
        return ws

    def test_filters_hold_time_and_profit(self):
        self.assertFalse(c.auto_follow(cand(holds=(30, 60, 90))))     # sells in < 5 min
        self.assertFalse(c.auto_follow(cand(pnl=-1)))                 # losing wallet
        self.assertFalse(c.auto_follow(cand(holds=(400,), trades=1)))  # not enough history
        self.assertTrue(c.auto_follow(cand()))

    def test_full_slots_keep_good_wallets(self):
        self.fill()
        self.assertFalse(c.auto_follow(cand()))
        self.assertFalse(c.replace_worst(cand()))   # nobody is bad yet

    def test_paused_or_losing_wallet_is_replaced(self):
        ws = self.fill()
        d = c._load()
        d["wallets"][ws[0]["wallet"]].update(paused=True, copies=6, pnl_usd=-3.0)
        c._save(d)
        new = cand()
        self.assertTrue(c.replace_worst(new))
        wallets = c._load()["wallets"]
        self.assertNotIn(ws[0]["wallet"], wallets)
        self.assertIn(new["wallet"], wallets)

    def test_wallet_with_open_copy_is_not_replaced(self):
        ws = self.fill()
        d = c._load()
        d["wallets"][ws[0]["wallet"]].update(paused=True)
        d["positions"]["MINT"] = {"wallet": ws[0]["wallet"], "real": False}
        c._save(d)
        self.assertFalse(c.replace_worst(cand()))


class TelegramMenuTests(unittest.TestCase):
    def test_menu_has_only_copy_trade_and_wallet(self):
        import telegram_bot as tb
        data = [b["callback_data"] for row in tb.keyboard() for b in row]
        self.assertIn("copy_sim", data)
        self.assertIn("copy_real", data)
        self.assertIn("copy_wallets", data)
        self.assertFalse([x for x in data if x.startswith(("btc", "vol_", "launch", "lev_"))])

    def test_discovery_text_without_rpc(self):
        import telegram_bot as tb
        with patch.dict(os.environ, {"SOLANA_RPC_URL": ""}):
            self.assertIn("SOLANA_RPC_URL", tb.discovery_text())


if __name__ == "__main__":
    unittest.main()
