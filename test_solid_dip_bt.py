import random
import time
import unittest

import solid_dip_bt as bt

H = 3600


def candles(prices, t0=0):
    return [{"t": t0 + i * H, "h": p * 1.001, "c": p} for i, p in enumerate(prices)]


class DipTest(unittest.TestCase):
    def test_tops_window_excludes_current(self):
        cs = candles([1, 5, 2, 3, 1])
        self.assertEqual(bt.tops(cs, 2)[3], 5 * 1.001)          # max of candles 1 and 2
        self.assertIsNone(bt.tops(cs, 2)[1])

    def test_buy_dip_sell_8pct_and_stuck(self):
        prices = [10] * 10 + [8.5] + [8.6] * 5 + [9.3] + [9.0] * 5     # -15%: buys at 8.5, +8% = 9.18 reached at 9.3
        data = {"AAA": candles(prices), "BBB": candles([10] * 10 + [8] + [5] * 11)}   # falls and never comes back
        s = bt.simulate(data, 0, 100 * H, 0.15, 5)
        self.assertEqual(s["trades"], 1)
        exp = 10 * (1 - bt.COST) / 8.5 * 8.5 * 1.08 * (1 - bt.COST) - 10
        self.assertAlmostEqual(s["closed_pnl"], round(exp, 2), places=2)
        self.assertEqual([o["coin"] for o in s["open"]], ["BBB"])      # no stop loss: stays open
        self.assertLess(s["open_pnl"], -3)
        self.assertGreater(s["dd"], 0)

    def test_cash_never_negative_and_report(self):
        rnd = random.Random(1)
        data = {}
        for c in ("SOL", "JUP", "BONK"):
            p, ps = 10.0, []
            for _ in range(24 * 120):
                p *= rnd.uniform(0.97, 1.03)
                ps.append(p)
            data[c] = candles(ps, 1_700_000_000)
        s = bt.simulate(data, 0, 2_000_000_000, 0.10, 72, size=10, capital=20)
        self.assertGreaterEqual(s["final"], 0)
        self.assertLessEqual(len(s["open"]), 2)
        get = lambda url: (_ for _ in ()).throw(RuntimeError("off"))
        self.assertIsNone(bt.run(now=1_800_000_000, get=get))
        self.assertIn("não consegui", bt.text(None))


if __name__ == "__main__":
    unittest.main()
