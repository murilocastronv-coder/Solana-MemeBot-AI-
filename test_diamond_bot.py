import pathlib
import tempfile
import time
import unittest
from unittest import mock

import diamond_bot as db
import live_exit_monitor as lem

NOW = 1_800_000_000
SOL = "So11111111111111111111111111111111111111112"


def pair(mint="M1", **kw):
    p = {"chainId": "solana", "baseToken": {"address": mint, "symbol": "TOK"}, "quoteToken": {"address": SOL},
         "liquidity": {"usd": 900_000}, "volume": {"h24": 5_000_000, "h6": 1_200_000},
         "pairCreatedAt": (NOW - 30 * 86400) * 1000, "marketCap": 80_000_000,
         "priceChange": {"m5": 0.4, "h1": -4, "h6": 2, "h24": 9},
         "txns": {"h24": {"buys": 6000, "sells": 5000}, "h1": {"buys": 300, "sells": 320}}}
    for k, v in kw.items():
        p[k] = v
    return p


class DiamondRulesTest(unittest.TestCase):
    def test_good_pullback_passes(self):
        self.assertEqual(db.reason(pair(), NOW), "")

    def test_each_rule_is_checked(self):
        self.assertTrue(db.reason(pair(liquidity={"usd": 100_000}), NOW).startswith("U1"))
        self.assertTrue(db.reason(pair(pairCreatedAt=(NOW - 86400) * 1000), NOW).startswith("U3"))
        self.assertTrue(db.reason(pair(priceChange={"m5": 1, "h1": -4, "h6": -2, "h24": -5}), NOW).startswith("T1"))
        self.assertTrue(db.reason(pair(volume={"h24": 5_000_000, "h6": 100_000}), NOW).startswith("T2"))
        self.assertTrue(db.reason(pair(priceChange={"m5": 1, "h1": 3, "h6": 2, "h24": 9}), NOW).startswith("E1"))
        self.assertTrue(db.reason(pair(priceChange={"m5": -1, "h1": -4, "h6": 2, "h24": 9}), NOW).startswith("E2"))

    def test_relaxed_pullback_after_idle(self):
        flat = pair(priceChange={"m5": 0.2, "h1": -0.5, "h6": 2, "h24": 9})
        self.assertTrue(db.reason(flat, NOW).startswith("E1"))
        self.assertEqual(db.reason(flat, NOW, relaxed=True), "")

    def test_exit_rules(self):
        pos = {"mode": "diamond", "peak_ratio": 0}
        self.assertEqual(lem.position_signal(pos, 30, 26.5)["action"], "sell")        # -11.7%
        self.assertEqual(lem.position_signal(pos, 30, 31)["action"], "hold")
        self.assertEqual(lem.position_signal({"mode": "diamond", "peak_ratio": 1.20}, 30, 33.6)["action"], "sell")


class DiamondCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(db, "_path", return_value=pathlib.Path(self.tmp.name) / "d.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_off_does_nothing(self):
        self.assertIsNone(db.cycle(pairs=[pair()], now=NOW))

    def test_buys_once_then_cooldown(self):
        db.start_round(NOW)
        buyer = mock.Mock(return_value="")
        self.assertEqual(db.cycle(pairs=[pair()], now=NOW + 900, safety=lambda m: "", buyer=buyer), "BUY TOK")
        self.assertTrue(db.cycle(pairs=[pair()], now=NOW + 1800, safety=lambda m: "", buyer=buyer).startswith("HOLD"))
        self.assertEqual(buyer.call_count, 1)
        self.assertIn("BUY TOK", db._load()["log"][0])

    def test_round_end_sells_and_stops(self):
        db.start_round(NOW)
        seller = mock.Mock()
        with mock.patch("live_trade_state._load", return_value={"positions": {"M1": {"mode": "diamond"}, "M2": {"mode": "rocket"}}}), \
                mock.patch.object(db, "status_text", return_value="ok"):
            self.assertEqual(db.cycle(pairs=[], now=NOW + db.ROUND_S + 1, seller=seller), "fim")
        seller.assert_called_once_with("M1", "diamond_round_end", None, None)
        self.assertFalse(db.enabled())


if __name__ == "__main__":
    unittest.main()


class DiamondSimulationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(db, "_path", return_value=pathlib.Path(self.tmp.name) / "d.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_paper_buy_and_stop_loss_with_real_prices(self):
        db.start_round(NOW)
        p = pair(priceUsd="2.0")
        self.assertEqual(db.cycle(pairs=[p], now=NOW + 60, safety=lambda m: ""), "BUY TOK")
        sim = db._load()["sim"]
        self.assertAlmostEqual(sim["cash"], db.SIM_START_USD - db.ORDER_USD)
        self.assertIn("M1", sim["positions"])
        crashed = pair(priceUsd="1.7", priceChange={"m5": -3, "h1": -20, "h6": -10, "h24": -10})
        db.cycle(pairs=[crashed], now=NOW + 120, safety=lambda m: "")
        sim = db._load()["sim"]
        self.assertEqual(sim["positions"], {})
        h = sim["history"][-1]
        self.assertEqual(h["reason"], "stop_loss")
        self.assertLess(h["pnl"], -4)

    def test_paper_trailing_takes_profit(self):
        db.start_round(NOW)
        db.sim_buy(pair(priceUsd="1.0"), now=NOW)
        db.sim_exits({"M1": 1.20}, now=NOW + 60)            # +20% peak
        self.assertIn("M1", db._load()["sim"]["positions"])
        closed = db.sim_exits({"M1": 1.10}, now=NOW + 120)  # > 6% under the top
        self.assertEqual(len(closed), 1)
        self.assertGreater(closed[0]["pnl"], 0)

    def test_round_end_closes_paper_positions(self):
        db.start_round(NOW)
        db.sim_buy(pair(priceUsd="1.0"), now=NOW)
        with mock.patch("live_trade_state._load", return_value={"positions": {}}), \
                mock.patch.object(db, "status_text", return_value="ok"):
            db.cycle(pairs=[], now=NOW + db.ROUND_S + 1)
        self.assertEqual(db._load()["sim"]["positions"], {})
        self.assertEqual(db._load()["sim"]["history"][-1]["reason"], "round_end")
