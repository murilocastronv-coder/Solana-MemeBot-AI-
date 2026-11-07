import random, time, unittest
from unittest.mock import patch
import backtest as bt
import btc_perps as b


def walk(n, secs, drift=0.0, vol=0.002, seed=1, start=60000.0):
    rnd = random.Random(seed)
    t0 = int(time.time()) // secs * secs - (n + 1) * secs
    out, p = [], start
    for i in range(n):
        o = p
        c = o * (1 + drift + rnd.gauss(0, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append({"t": t0 + i * secs, "o": o, "h": h, "l": l, "c": c}); p = c
    return out


class BacktestTests(unittest.TestCase):
    def test_scalp_runs_fast_on_30_days_and_charges_fees(self):
        m5 = walk(30 * 288 + 150, 300, vol=0.0025)
        t = time.time()
        sigs = bt.scalp_signals(m5)
        results = [bt.run_scalp(m5, lev, sigs=sigs) for lev in bt.LEVERAGES]
        self.assertLess(time.time() - t, 30)
        r3 = results[1]
        self.assertGreater(r3["trades"], 0)
        self.assertGreater(r3["fees"], 0)
        self.assertGreater(results[3]["fees"], r3["fees"])      # more leverage, more fees
        self.assertGreater(r3["final"], 0)

    def test_flat_market_no_trades(self):
        flat = [{"t": i * 300, "o": 60000.0, "h": 60001.0, "l": 59999.0, "c": 60000.0} for i in range(600)]
        self.assertEqual(bt.run_scalp(flat, 3)["trades"], 0)
        self.assertEqual(bt.run_trend(flat, 3)["trades"], 0)

    def test_trend_profits_in_strong_trend(self):
        h1 = walk(30 * 24 + 260, 3600, drift=0.002, vol=0.003, seed=7)
        r = bt.run_trend(h1, 2)
        self.assertGreater(r["trades"], 0)
        self.assertGreater(r["pnl_pct"], 0)

    def test_no_look_ahead_entry_is_next_open(self):
        acc = bt.Account(2)
        pos = acc.open("long", 100.0, 1.0, 1.0)
        self.assertEqual(pos["entry"], 100.0)
        acc.close(pos, 100.0, 0)
        self.assertAlmostEqual(acc.trades[0], -pos["size"] * b.FEE_RATE * 2)   # flat trade only pays fees

    def test_report_text(self):
        text = bt.report_text(walk(1000, 300, vol=0.0025), walk(600, 3600, vol=0.004), days=3)
        self.assertIn("SCALPING M5", text)
        self.assertIn("TENDÊNCIA 1h", text)
        self.assertIn("10x:", text)

    def test_fetch_history_paginates_binance(self):
        now = int(time.time()) // 300 * 300
        calls = []
        def fake_get(url):
            calls.append(url)
            end = int(url.split("endTime=")[1]) // 1000 // 300 * 300 if "endTime" in url else now
            return [[(end - (999 - k) * 300 - 600) * 1000, "1", "2", "0.5", "1.5"] for k in range(1000)]
        with patch("btc_perps._get", side_effect=fake_get):
            rows = bt.fetch_history("5m", days=7, extra=100)
        self.assertEqual(len(rows), 7 * 288 + 100)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(len({r["t"] for r in rows}), len(rows))
        self.assertTrue(all(a["t"] < b2["t"] for a, b2 in zip(rows, rows[1:])))


if __name__ == "__main__":
    unittest.main()
