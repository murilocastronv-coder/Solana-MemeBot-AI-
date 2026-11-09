import unittest

import btc_backtest as bt
import btc_perps as bp


def series(prices, start=1_700_000_000):
    out = []
    for i, p in enumerate(prices):
        prev = prices[i - 1] if i else p
        out.append({"t": start + i * 3600, "o": prev, "h": max(p, prev) * 1.00005, "l": min(p, prev) * 0.99995, "c": p})
    return out


class BacktestTests(unittest.TestCase):
    def test_uptrend_makes_money_and_leverage_amplifies(self):
        prices = [60000 + i * 5 for i in range(300)] + [61500 + i * 40 for i in range(400)]
        candles = series(prices)
        low = bt.run(candles, 2, test_hours=400)
        high = bt.run(candles, 10, test_hours=400)
        self.assertGreater(low["final"], bt.START_USD)
        self.assertGreater(high["final"], low["final"])

    def test_crash_after_breakout_hits_stop_or_liquidation(self):
        # breakout long, then a sudden 11% drop and a flat market: the long is stopped out hard
        prices = [60000 + i * 5 for i in range(300)] + [61600] + [55000] * 40
        candles = series(prices)
        r = bt.run(candles, 20, fraction=1.0, test_hours=41)
        self.assertLess(r["final"], bt.START_USD)
        self.assertGreaterEqual(r["trades"], 1)

    def test_never_loses_more_than_collateral_and_report_text(self):
        prices = [60000 + i * 5 for i in range(300)] + [61600 - i * 900 for i in range(40)]
        candles = series(prices)
        r = bt.run(candles, 20, fraction=0.2, test_hours=60)
        self.assertGreater(r["final"], bt.START_USD * 0.79)   # one trade can lose at most its 20% collateral
        text = bt.report(series([60000 + i * 3 for i in range(bp.EMA_LEN + 800)]))
        self.assertIn("BACKTEST BTC ALAVANCADO", text)
        self.assertIn("20x", text)


if __name__ == "__main__":
    unittest.main()
