import os, tempfile, time, unittest
from unittest.mock import patch
import btc_perps as b


def series(closes, spread=50, shift=0):
    t0 = int(time.time()) // 3600 * 3600 - len(closes) * 3600 - 3600 + shift
    return [{"t": t0 + i * 3600, "o": c, "h": c + spread, "l": c - spread, "c": c} for i, c in enumerate(closes)]


class BTCTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(self.dir.name, "w.json")})
        self.env.start()
        self.sent = []

    def tearDown(self):
        self.env.stop(); self.dir.cleanup()

    def send(self, chat, text, *a):
        self.sent.append(text)

    def test_signals(self):
        up = series([60000 + i * 10 for i in range(230)] + [62600])
        self.assertEqual(b.signal(up)[0], "long")
        down = series([70000 - i * 10 for i in range(230)] + [67300])
        self.assertEqual(b.signal(down)[0], "short")
        flat = series([60000] * 231)
        self.assertIsNone(b.signal(flat)[0])
        self.assertIsNone(b.signal(series([60000] * 50))[0])   # not enough history

    def test_off_by_default(self):
        b.cycle(self.send, "1", candles=series([60000 + i * 10 for i in range(230)] + [62600]), price=62600)
        self.assertIsNone(b.summary()["position"])

    def test_long_trade_with_trailing_stop(self):
        b.set_enabled(True); b.set_leverage(3)
        candles = series([60000 + i * 10 for i in range(230)] + [62600])
        b.cycle(self.send, "1", candles=candles, price=62600)
        pos = b.summary()["position"]
        self.assertEqual((pos["side"], pos["leverage"], pos["collateral"], pos["size_usd"]), ("long", 3, 20.0, 60.0))
        self.assertLess(pos["stop"], 62600)
        self.assertGreater(pos["stop"], b._liq_price(pos))          # stop before liquidation
        self.assertIn("LONG BTC 3x", self.sent[-1])
        b.cycle(self.send, "1", candles=candles, price=66000)       # runs up
        b.cycle(self.send, "1", candles=candles, price=65000)       # pulls back below trailing stop
        s = b.summary()
        self.assertIsNone(s["position"])
        t = s["history"][-1]
        self.assertEqual(t["reason"], "trailing")
        self.assertGreater(t["pnl_usd"], 0)
        self.assertAlmostEqual(s["equity"], 100 + t["pnl_usd"], places=4)

    def test_high_leverage_stop_is_before_liquidation(self):
        b.set_enabled(True); b.set_leverage(20)
        candles = series([60000 + i * 10 for i in range(230)] + [64000], spread=900)  # very volatile
        b.cycle(self.send, "1", candles=candles, price=64000)
        pos = b.summary()["position"]
        self.assertGreater(pos["stop"], b._liq_price(pos))
        b.cycle(self.send, "1", candles=candles, price=pos["stop"] - 1)
        self.assertEqual(b.summary()["history"][-1]["reason"], "stop_loss")   # stopped, not liquidated

    def test_liquidation_on_gap(self):
        b.set_enabled(True); b.set_leverage(10)
        candles = series([60000 + i * 10 for i in range(230)] + [62600])
        b.cycle(self.send, "1", candles=candles, price=62600)
        b.cycle(self.send, "1", candles=candles, price=55000)   # gap far through stop
        t = b.summary()["history"][-1]
        self.assertEqual(t["reason"], "liquidated")
        self.assertAlmostEqual(t["pnl_usd"], -20.0)

    def test_reversal_long_to_short(self):
        b.set_enabled(True)
        b.cycle(self.send, "1", candles=series([60000 + i * 10 for i in range(230)] + [62600]), price=62600)
        with patch("btc_perps.manage"):  # isolate the signal logic
            b.cycle(self.send, "1", candles=series([70000 - i * 10 for i in range(230)] + [67300], shift=3600), price=62500)
        s = b.summary()
        self.assertEqual(s["history"][-1]["reason"], "reversal")
        self.assertEqual(s["position"]["side"], "short")

    def test_invalid_leverage(self):
        with self.assertRaises(ValueError):
            b.set_leverage(50)


class TelegramBTCTests(unittest.TestCase):
    def test_buttons(self):
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}):
            import telegram_bot as t, telegram_owner
            telegram_owner.claim(5)
            out = []
            with patch.object(t, "send", side_effect=lambda c, x, k=None: out.append((x, k))), \
                 patch.object(t, "request_api", return_value={}), \
                 patch("btc_perps.fetch_price", return_value=65000.0):
                cb = lambda a: t.handle({"callback_query": {"id": "1", "data": a, "message": {"chat": {"id": 5, "type": "private"}}}})
                # Old BTC/Perps controls were retired in favour of the MEV menu.
                for action in ("lev", "lev_10", "btc_on", "btc_status"):
                    cb(action)
                    self.assertIn("desativados", out[-1][0])
                labels = [b["text"] for row in t.keyboard() for b in row]
                self.assertIn("🎯 Sniper ON", labels)


if __name__ == "__main__":
    unittest.main()
