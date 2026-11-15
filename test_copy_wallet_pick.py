import os
import tempfile
import unittest
from unittest.mock import patch

ADDR = "DNMUwKFS1hsrEHoWchxHAptbb9QcPDYo2rLeoShugfni"
OTHER = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


class OwnerWalletPickTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"), "COPY_STATE_FILE": os.path.join(d, "c.json")})
        p.start()
        self.addCleanup(p.stop)
        import copy_trader
        self.ct = copy_trader

    def test_add_list_remove(self):
        ct = self.ct
        with patch.object(ct, "_path", return_value=__import__("pathlib").Path(tempfile.mkdtemp()) / "c.json"):
            ct.add_owner_wallet(OTHER)
            self.assertTrue(ct.mirror(OTHER))
            text = ct.picks_text()
            self.assertIn("`" + OTHER + "`", text)
            self.assertIn("sua escolha", text)
            self.assertTrue(ct.unfollow(OTHER))
            self.assertNotIn(OTHER, ct.picks_text())

    def test_invalid_address(self):
        with self.assertRaises(Exception):
            self.ct.add_owner_wallet("not-a-wallet")

    def test_telegram_accepts_plain_address_after_button(self):
        import telegram_bot as tb
        self.assertTrue(tb.SOL_ADDRESS.fullmatch(ADDR))
        self.assertFalse(tb.SOL_ADDRESS.fullmatch("/start"))


if __name__ == "__main__":
    unittest.main()


class ReferenceAndCounterTest(unittest.TestCase):
    def test_reference_comes_back_unless_owner_removed_and_counter(self):
        import pathlib
        import copy_trader as ct
        import wallet_twin
        p = pathlib.Path(tempfile.mkdtemp()) / "c.json"
        with patch.object(ct, "_path", return_value=p), patch.object(wallet_twin, "REFERENCE", ADDR), \
                patch.object(wallet_twin, "ENABLED", True):
            self.assertTrue(ct.ensure_reference())
            self.assertIn(ADDR, ct._load()["wallets"])
            self.assertTrue(ct.mirror(ADDR))
            ct.unfollow(ADDR)
            ct.mark_owner_removed(ADDR)
            self.assertFalse(ct.ensure_reference())          # the owner removed it: stays out
            with patch.object(ct, "_preaudit_and_follow", side_effect=["rejected", "added", "full"]):
                for _ in range(3):
                    ct.preaudit_and_follow({"wallet": "x"}, None, 200.0)
            self.assertIn("analisadas até agora: 2", ct.search_line())
            self.assertIn("aprovadas 1", ct.search_line())


class ResetFoundTest(unittest.TestCase):
    def test_found_wallets_leave_owner_and_reference_stay(self):
        import pathlib
        import copy_trader as ct
        p = pathlib.Path(tempfile.mkdtemp()) / "c.json"
        with patch.object(ct, "_path", return_value=p):
            ct.add_owner_wallet(OTHER)
            ct.follow(ADDR)
            d = ct._load()
            d["wallets"][ADDR]["auto_discovered"] = True
            d["rejected"] = {"X": 10**12}
            ct._save(d)
            self.assertEqual(ct.reset_found_wallets_once(), [ADDR])
            d = ct._load()
            self.assertNotIn(ADDR, d["wallets"])
            self.assertIn(OTHER, d["wallets"])
            self.assertEqual(d["rejected"], {})
            self.assertEqual(ct.reset_found_wallets_once(), [])


class DropReferenceTest(unittest.TestCase):
    def test_reference_removed_once_and_not_put_back(self):
        import pathlib
        import copy_trader as ct
        import wallet_twin
        p = pathlib.Path(tempfile.mkdtemp()) / "c.json"
        with patch.object(ct, "_path", return_value=p), patch.object(wallet_twin, "REFERENCE", ADDR), \
                patch.object(wallet_twin, "ENABLED", True):
            ct.ensure_reference()
            self.assertTrue(ct.drop_reference_once())
            self.assertFalse(ct.ensure_reference())
            self.assertNotIn(ADDR, ct._load()["wallets"])
            self.assertFalse(ct.drop_reference_once())


class MinimumBuyTest(unittest.TestCase):
    def test_small_share_is_raised_to_the_minimum(self):
        import copy_trader as ct
        with patch.object(ct, "PROPORTIONAL_MIN_USD", 20.0):
            self.assertEqual(ct.proportional_usd(0.01, 150, 100, limits=(0.05, 0.06)), 20.0)   # 5% = 7.5 -> 20
            self.assertIsNone(ct.proportional_usd(0.01, 150, 15, limits=(0.05, 0.06)))       # only 13 free: skip


class EvaluateWalletTest(unittest.TestCase):
    def test_report_says_if_worth_copying(self):
        import copy_trader as ct
        import wallet_twin as wt
        style = {"hold_min": 45.0, "win_rate": 0.6, "trades_day": 10, "size_usd": 50, "pump_share": 0.9,
                 "curve_share": 0.1, "entry_age_min": 30}
        good = {"pnl_recent": 2500, "win_recent": 0.6, "trades_recent": 80, "weeks_with_trades": 4,
                "weeks_positive": 4, "style": style, "roi_recent": 0.35, "invested_recent": 7000, "rug_share": 0.05,
                "rugs_recent": 4, "hold_median_min": 40, "hold_max_min": 900, "avg_win_pct": 0.6, "avg_loss_pct": -0.3}
        with patch.object(wt, "MIN_PROFIT", 1000.0), patch.object(ct, "MIN_ACTIVE_DAYS_30", 25), \
                patch.object(ct, "MIN_WALLET_DAYS", 30):
            ok, text = ct.evaluate_wallet(OTHER, None, 200, measure_fn=lambda *a: ([], good),
                                          activity_fn=lambda a: {"age_days": 90, "active_days_30": 28},
                                          balance_fn=lambda a: 5000)
            self.assertTrue(ok)
            self.assertIn("VALE A PENA COPIAR", text)
            self.assertIn("Retorno sobre o que investiu: +35%", text)
            self.assertIn("golpe", text)
            self.assertIn("metade das moedas até 40 min", text)
            bad = dict(good, pnl_recent=200, weeks_positive=2)
            ok, text = ct.evaluate_wallet(OTHER, None, 200, measure_fn=lambda *a: ([], bad),
                                          activity_fn=lambda a: {"age_days": 90, "active_days_30": 10},
                                          balance_fn=lambda a: 5000)
            self.assertFalse(ok)
            self.assertIn("NÃO VALE", text)
            self.assertIn("❌ Dias operando", text)



class MoneyStatsTest(unittest.TestCase):
    def test_roi_rugs_and_hold(self):
        import copy_trader as ct
        recent = [{"cost_closed": 100, "pnl_usd": 50, "result_pct": 0.5, "hold_min": 30},
                  {"cost_closed": 100, "pnl_usd": -90, "result_pct": -0.9, "hold_min": 300},
                  {"cost_closed": 100, "pnl_usd": 20, "result_pct": 0.2, "hold_min": 15}]
        m = ct.money(recent, [])
        self.assertAlmostEqual(m["roi_recent"], -20 / 300, places=3)
        self.assertEqual(m["rugs_recent"], 1)
        self.assertEqual(m["hold_median_min"], 30)
