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
        out.append({"t": t0 + i * secs, "o": o, "h": h, "l": l, "c": c, "v": rnd.uniform(50, 150)}); p = c
    return out


class BacktestTests(unittest.TestCase):
    def test_scalp_runs_fast_on_30_days_and_charges_costs(self):
        m5 = walk(30 * 288 + 80, 300, vol=0.0025)
        t = time.time()
        text = bt.report_text(m5)
        self.assertLess(time.time() - t, 30)
        r = bt.run_scalp(m5, 1)
        self.assertGreater(r["trades"], 0)
        self.assertAlmostEqual(r["gross_usd"] - r["fees"], r["pnl_usd"], places=6)
        stress = bt.run_scalp(m5, 1, fee_mult=2, slip_mult=2)
        self.assertLess(stress["pnl_usd"], r["pnl_usd"])           # higher costs, worse result
        self.assertIn("P/L TOTAL", text)

    def test_stop_counts_first_when_both_touched(self):
        c = [{"t": i * 300, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1} for i in range(70)]
        sigs = [(None, 0, "")] * 69 + [("long", 1, "")]
        sigs[60] = ("long", 1.0, "")
        r = bt.run_scalp(c, 1, sigs=sigs)
        self.assertEqual(r["trades"], 1)
        self.assertLess(r["pnl_usd"], 0)                            # worst case assumed

    def test_flat_market_no_trades(self):
        flat = [{"t": i * 300, "o": 60000.0, "h": 60001.0, "l": 59999.0, "c": 60000.0, "v": 100.0} for i in range(600)]
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
        text = bt.report_text(walk(1000, 300, vol=0.0025), days=3)
        for part in ("ESTRATÉGIA M5", "P/L TOTAL", "SL 0.15% / TP", "taxas e deslize dobrados", "BTC no período"):
            self.assertIn(part, text)

    def test_fetch_history_paginates_binance(self):
        now = int(time.time()) // 300 * 300
        calls = []
        def fake_get(url):
            calls.append(url)
            end = int(url.split("endTime=")[1]) // 1000 // 300 * 300 if "endTime" in url else now
            return [[(end - (999 - k) * 300 - 600) * 1000, "1", "2", "0.5", "1.5", "10"] for k in range(1000)]
        with patch("btc_perps._get", side_effect=fake_get):
            rows = bt.fetch_history("5m", days=7, extra=100)
        self.assertEqual(len(rows), 7 * 288 + 100)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(len({r["t"] for r in rows}), len(rows))
        self.assertTrue(all(a["t"] < b2["t"] for a, b2 in zip(rows, rows[1:])))


if __name__ == "__main__":
    unittest.main()
