import os
import tempfile
import time
import unittest
from unittest.mock import patch

import btc_bot as bb

NOW = time.time()


def candles(up5=0.006, vol_mult=2.5, buy_share=0.7, trend_up=True):
    cs, price, t = [], 100_000.0, NOW - 90 * 60
    for i in range(84):                                   # calm hour, slightly up (or down)
        price *= 1.0002 if trend_up else 0.9998
        cs.append((t, price, price, price, price, 10.0, 5.0))
        t += 60
    step = (1 + up5) ** (1 / 5)
    for i in range(5):                                    # buyers flood in
        price *= step
        cs.append((t, price, price, price, price, 10.0 * vol_mult, 10.0 * vol_mult * buy_share))
        t += 60
    cs.append((t, price, price, price, price, 1.0, 0.5))  # forming candle
    return cs


class BtcTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_entry_when_many_buyers(self):
        self.assertEqual(bb.entry_reason(candles()), "")
        self.assertIn("sem subida", bb.entry_reason(candles(up5=0.001)))
        self.assertIn("vertical", bb.entry_reason(candles(up5=0.03)))
        self.assertIn("pouca gente", bb.entry_reason(candles(vol_mult=1.1)))
        self.assertIn("compradores fracos", bb.entry_reason(candles(buy_share=0.5)))

    def test_exit_rules_on_the_20x_position(self):
        pos = {"entry": 100_000, "size_usd": 400, "collateral_usd": 20, "at": NOW, "peak": 1.0}
        self.assertEqual(bb.exit_reason(dict(pos), 99_000, NOW)[0], "")             # -1% BTC = -20%-ish: hold
        self.assertEqual(bb.exit_reason(dict(pos), 98_400, NOW)[0], "stop_loss")    # -1.6% BTC: past -30%
        p = dict(pos, peak=1.5)
        self.assertEqual(bb.exit_reason(p, 101_400, NOW)[0], "trailing")            # 1.5 -> ~1.26 (>10% off the top)
        old = dict(pos, at=NOW - 61 * 60)
        self.assertEqual(bb.exit_reason(old, 100_200, NOW)[0], "time_stop")

    def test_open_and_close_cycle(self):
        sent, opened, closed = [], [], []
        opener = lambda price: opened.append(price) or {"entry": price, "size_usd": 400, "collateral_usd": 20}
        self.assertEqual(bb.cycle(lambda c, t: sent.append(t), 1, cs=candles(), now=NOW, opener=opener), "opened")
        self.assertTrue(bb._load()["open"])
        self.assertIn("COMPROU BTC 20x", sent[-1])
        cs = candles()
        crash = cs[:-1] + [(NOW, 1, 1, 1, cs[-1][4] * 0.98, 1, 0.5)]
        self.assertEqual(bb.cycle(lambda c, t: sent.append(t), 1, cs=crash, now=NOW + 60,
                                  closer=lambda p: closed.append(p) or {}), "closed")
        self.assertIsNone(bb._load()["open"])
        self.assertIn("PREJUÍZO", sent[-1])
        self.assertEqual(bb.cycle(None, None, cs=candles(), now=NOW + 120, opener=opener), "")   # cooldown

    def test_pauses_after_5_losses(self):
        d = bb._load()
        d["history"] = [{"pnl_usd": -6}] * 4
        d["open"] = {"entry": 100_000, "size_usd": 400, "collateral_usd": 20, "at": NOW, "peak": 1.0}
        bb._save(d)
        cs = candles()
        crash = cs[:-1] + [(NOW, 1, 1, 1, 97_000, 1, 0.5)]
        bb.cycle(None, None, cs=crash, now=NOW, closer=lambda p: {})
        self.assertFalse(bb.enabled())

    def test_status(self):
        self.assertIn("BTC 20x", bb.status_text())


if __name__ == "__main__":
    unittest.main()
