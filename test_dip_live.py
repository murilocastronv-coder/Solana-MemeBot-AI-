import os, tempfile, unittest
from unittest.mock import patch
import btc_perps as b
from test_backtest import dip_series


class DipLiveTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"), "BTC_SIM_START_USD": "150"})
        self.env.start(); self.addCleanup(self.env.stop)
        data = b._load(); data.update(balance=150.0, start=150.0); b._save(data)
        b.set_enabled(True); b.set_option("strategy", "dip")
        self.sent = []
        self.c = dip_series([])
        self.bottom = self.c[-1]["c"]

    def send(self, chat, text, *a):
        self.sent.append(text)

    def test_buy_rebuy_and_close_at_3(self):
        b.cycle(self.send, "1", candles=self.c, price=self.bottom)
        pos = b.summary()["position"]
        self.assertEqual((len(pos["entries"]), pos["collateral"], pos["size_usd"], pos["side"]), (1, 25.0, 125.0, "long"))
        self.assertIn("COMPRA NO FUNDO", self.sent[-1])
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 0.99)        # -1%: not enough (gap is 2%)
        self.assertEqual(len(b.summary()["position"]["entries"]), 1)
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 0.979)       # -2.1% -> re-buy
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 0.958)       # -2% again -> 3rd buy
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 0.937)       # max 3: no 4th buy
        pos = b.summary()["position"]
        self.assertEqual(len(pos["entries"]), 3)
        self.assertEqual(b._load()["balance"], 75.0)
        target_price = pos["entry"] * (1 + (3.0 + pos["size_usd"] * b.FEE_RATE * 2) / pos["size_usd"]) * 1.0001
        b.cycle(self.send, "1", candles=self.c, price=target_price)
        s = b.summary()
        self.assertIsNone(s["position"])
        self.assertGreaterEqual(s["history"][-1]["pnl_usd"], 3.0)
        self.assertIn("3 compras", s["history"][-1]["symbol"])

    def test_single_buy_needs_2_50(self):
        b.cycle(self.send, "1", candles=self.c, price=self.bottom)
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 1.016)     # +$2 gross: not yet
        self.assertIsNotNone(b.summary()["position"])
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 1.0225)
        self.assertGreaterEqual(b.summary()["history"][-1]["pnl_usd"], 2.5)

    def test_emergency_stop(self):
        b.cycle(self.send, "1", candles=self.c, price=self.bottom)
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 0.92)       # -8% at once
        s = b.summary()
        self.assertIsNone(s["position"])
        self.assertEqual(s["history"][-1]["reason"], "emergency")

    def test_never_shorts(self):
        up = [{**c, "o": 2 * self.bottom - c["c"], "c": 2 * self.bottom - c["o"]} for c in self.c]
        b.cycle(self.send, "1", candles=up, price=up[-1]["c"])
        pos = b.summary()["position"]
        self.assertTrue(pos is None or pos["side"] == "long")


if __name__ == "__main__":
    unittest.main()
