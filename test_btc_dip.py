import unittest
from unittest.mock import patch

import btc_dip as bd

T0 = 1_700_000_000 // 3600 * 3600


def path(prices):
    """Hourly candles walking linearly between the given prices (10 hours per leg)."""
    pts, out = [], []
    for a, b in zip(prices, prices[1:]):
        pts += [a + (b - a) * k / 10 for k in range(10)]
    pts.append(prices[-1])
    for i, p in enumerate(pts):
        prev = pts[i - 1] if i else p
        out.append({"t": T0 + i * 3600, "o": prev, "h": max(prev, p), "l": min(prev, p), "c": p, "v": 1})
    return out


class DipTests(unittest.TestCase):
    def test_two_buys_then_sell_all_at_plus_7(self):
        cs = path([100000, 100000, 80000, 70000, 90000])
        r = bd.backtest(cs, start=11)            # top 100k from the first leg
        self.assertEqual(len(r["cycles"]), 1)
        x = r["cycles"][0]
        self.assertEqual([round(p) for _, p in x["buys"]], [85000, 72250])
        avg = x["avg"]
        self.assertAlmostEqual(x["exit"], avg * 1.07, delta=1)
        self.assertGreater(x["pnl"], 0)
        self.assertIsNone(r["open"])

    def test_at_most_three_buys_and_open_at_the_end(self):
        cs = path([100000, 100000, 40000])
        r = bd.backtest(cs, start=11)
        self.assertEqual(len(r["open"]["buys"]), 3)
        self.assertLess(r["total"], 0)

    def test_leverage_gets_liquidated_in_a_long_fall(self):
        cs = path([100000, 100000, 40000])
        r = bd.backtest(cs, lev=5, start=11)
        self.assertGreaterEqual(r["liquidations"], 1)

    def test_no_drop_no_buy(self):
        cs = path([100000, 100000, 95000, 105000])
        r = bd.backtest(cs, start=11)
        self.assertEqual((r["cycles"], r["open"]), ([], None))
        self.assertIn("Nenhuma compra", bd.report_text([r]))

    def test_report(self):
        cs = path([100000, 100000, 80000, 70000, 90000])
        text = bd.run(cs)
        self.assertIn("COMPRA NO FUNDO", text)
        self.assertIn("5x", text)


if __name__ == "__main__":
    unittest.main()
