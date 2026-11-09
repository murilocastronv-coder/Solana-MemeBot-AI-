import os
import tempfile
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import copy_trader as c

REAL_WALLET_USD = c.wallet_usd


def setUpModule():
    # every wallet in these tests has a big enough balance unless a test says otherwise
    for p in (patch.object(c, "wallet_usd", return_value=1000.0), patch.object(c, "_sol_usd", return_value=200.0)):
        p.start()
        unittest.addModuleCleanup(p.stop)


def cand(pnl=10**9, holds=(400, 500, 600), trades=5):
    return {"wallet": str(Keypair().pubkey()), "closed_trades": trades, "realized_lamports": pnl,
            "hold_samples": list(holds), "average_buy_lamports": 10**9,   # 1 SOL per buy
            "age_days": 90, "active_days_7": 7}


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


class MinBalanceTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        c._balance_cache.clear()

    def test_small_average_buy_is_not_followed(self):
        small = cand()
        small["average_buy_lamports"] = 10**8          # 0.1 SOL x $200 = $20 per buy
        self.assertFalse(c.auto_follow(small))
        unknown = cand()
        unknown.pop("average_buy_lamports")
        self.assertFalse(c.auto_follow(unknown))
        self.assertTrue(c.auto_follow(cand()))           # 1 SOL = $200 per buy

    def test_young_or_unknown_age_wallet_is_not_followed(self):
        young = cand()
        young["age_days"] = 3
        self.assertFalse(c.auto_follow(young))
        unknown = cand()
        unknown["age_days"] = None
        self.assertFalse(c.auto_follow(unknown))
        self.assertTrue(c.auto_follow(cand()))
        self.assertIn("ativa há 90+ dias", c.wallets_text())

    def test_wallet_that_does_not_trade_daily_is_not_followed(self):
        lazy = cand()
        lazy["active_days_7"] = 3
        self.assertFalse(c.auto_follow(lazy))
        ok = cand()
        ok["active_days_7"] = 6
        self.assertTrue(c.auto_follow(ok))
        self.assertIn("operou 6/7 dias", c.wallets_text())

    def test_min_is_150_usd(self):
        self.assertEqual(c.MIN_WALLET_USD, 150)

    def test_poor_wallet_is_not_followed(self):
        with patch.object(c, "wallet_usd", return_value=90.0):
            self.assertFalse(c.auto_follow(cand()))
        with patch.object(c, "wallet_usd", return_value=None):   # balance unknown: wait
            self.assertFalse(c.auto_follow(cand()))
        with patch.object(c, "wallet_usd", return_value=151.0):
            self.assertTrue(c.auto_follow(cand()))

    def test_wallet_below_min_is_removed_unless_copy_open(self):
        rich, poor, busy = cand(), cand(), cand()
        for w in (rich, poor, busy):
            c.auto_follow(w)
        d = c._load()
        d["positions"]["MINT"] = {"wallet": busy["wallet"], "real": False}
        c._save(d)
        bal = {rich["wallet"]: 500.0, poor["wallet"]: 40.0, busy["wallet"]: 10.0}
        with patch.object(c, "wallet_usd", side_effect=lambda a: bal[a]):
            removed = c.prune_low_balance()
        self.assertEqual(removed, [poor["wallet"]])
        wallets = c._load()["wallets"]
        self.assertIn(busy["wallet"], wallets)      # stays until its copy is closed
        self.assertEqual(wallets[rich["wallet"]]["balance_usd"], 500.0)
        self.assertIn("saldo US$ 500", c.wallets_text())

    def test_wallet_usd_reads_rpc_balance(self):
        real = REAL_WALLET_USD
        with patch.object(c, "_rpc", return_value={"value": 2 * 10**9}), patch.object(c, "_sol_usd", return_value=100.0):
            self.assertEqual(real("X"), 200.0)
        with patch.object(c, "_rpc", side_effect=RuntimeError("down")):
            self.assertIsNone(real("Y"))


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


class SelfAnalysisTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def c(self, sol, win=0.8, trades=5):
        x = cand(pnl=int(sol * 1e9), trades=trades)
        x["win_rate"] = win
        return x

    def test_limit_is_15_by_default(self):
        self.assertEqual(c.MAX_AUTO_WALLETS, 15)
        for _ in range(15):
            self.assertTrue(c.auto_follow(self.c(0.01)))
        self.assertFalse(c.auto_follow(self.c(0.01)))

    def test_clearly_better_wallet_replaces_weakest_unproven(self):
        ws = [self.c(0.01) for _ in range(c.MAX_AUTO_WALLETS)]
        ws[3] = self.c(0.001)
        for w in ws:
            c.auto_follow(w)
        self.assertFalse(c.replace_worst(self.c(0.0012)))   # not 1.5x better
        strong = self.c(0.5, win=0.9, trades=15)
        self.assertTrue(c.replace_worst(strong))
        wallets = c._load()["wallets"]
        self.assertNotIn(ws[3]["wallet"], wallets)
        self.assertIn(strong["wallet"], wallets)

    def test_proven_wallet_is_kept(self):
        ws = [self.c(0.01) for _ in range(c.MAX_AUTO_WALLETS)]
        for w in ws:
            c.auto_follow(w)
        d = c._load()
        for w in d["wallets"].values():
            w.update(copies=6, pnl_usd=4.0)       # all proven profitable for us
        c._save(d)
        self.assertFalse(c.replace_worst(self.c(5.0)))

    def test_wallets_text_lists_count_and_links(self):
        w = self.c(0.2)
        c.auto_follow(w)
        text = c.wallets_text()
        self.assertIn("1 ativas de 1", text)
        self.assertIn("solscan.io/account/" + w["wallet"], text)

    def test_refresh_updates_score(self):
        w = self.c(0.01)
        c.auto_follow(w)
        better = dict(w, realized_lamports=int(1e9), win_rate=0.9)
        self.assertTrue(c.refresh_stats(better))
        self.assertGreater(c._load()["wallets"][w["wallet"]]["score"], 0.5)


class NoticeTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_card_has_link_and_stats(self):
        w = cand()
        w.update(win_rate=0.8, average_hold_seconds=1800)
        c.auto_follow(w)
        card = c.wallet_card(w["wallet"])
        for piece in ("solscan.io/account/" + w["wallet"], "acerto 80%", "segura em média 30 min",
                      "compra média 1.00 sol", "opera há 90+ dias", "ativa 7/7 dias", "saldo US$ 1000"):
            self.assertIn(piece.lower(), card.lower())

    def test_card_says_which_wallet_was_replaced(self):
        ws = [cand() for _ in range(c.MAX_AUTO_WALLETS)]
        for w in ws:
            c.auto_follow(w)
        d = c._load()
        d["wallets"][ws[0]["wallet"]].update(paused=True)
        c._save(d)
        new = cand()
        self.assertTrue(c.replace_worst(new))
        self.assertIn("Entrou no lugar de " + ws[0]["wallet"][:6], c.wallet_card(new["wallet"]))
