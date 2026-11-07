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


def dip_series(after, t0=1_700_000_000):
    """60 flat candles, a 2.4% drop in red candles, one green bottom candle, then `after` closes."""
    out, p = [], 60000.0
    for i in range(60):
        o = p; c = p + (5 if i % 2 else -5)
        out.append({"t": t0 + i * 300, "o": o, "h": max(o, c) + 5, "l": min(o, c) - 5, "c": c, "v": 1}); p = c
    for k in range(6):
        o = p; c = p * 0.996
        out.append({"t": t0 + len(out) * 300, "o": o, "h": o + 1, "l": c - 1, "c": c, "v": 1}); p = c
    o = p; c = p * 1.0005
    out.append({"t": t0 + len(out) * 300, "o": o, "h": c + 1, "l": o - 1, "c": c, "v": 1}); p = c
    for target in after:
        o = p; c = target
        out.append({"t": t0 + len(out) * 300, "o": o, "h": max(o, c) + 1, "l": min(o, c) - 1, "c": c, "v": 1}); p = c
    return out


class DipBacktestTests(unittest.TestCase):
    def test_signal_fires_at_bottom(self):
        c = dip_series([])
        self.assertTrue(b.dip_signal(c)[0])
        self.assertFalse(b.dip_signal(c[:-1])[0])                       # red candle: wait for green

    def test_single_buy_closes_at_2_50(self):
        c = dip_series([])
        bottom = c[-1]["c"]
        c = dip_series([bottom * 1.006, bottom * 1.012, bottom * 1.024])
        r = bt.run_dip(c)
        self.assertEqual((r["cycles"], r["by_buys"][1]), (1, 1))
        self.assertAlmostEqual(r["pnl_usd"], 2.50, places=2)

    def test_three_buys_close_at_3(self):
        bottom = dip_series([])[-1]["c"]
        path = [bottom * 0.99, bottom * 0.979, bottom * 0.968, bottom * 0.958, bottom * 0.975, bottom * 0.99, bottom * 1.005]
        r = bt.run_dip(dip_series(path))
        self.assertEqual((r["cycles"], r["by_buys"][3]), (1, 1))
        self.assertAlmostEqual(r["pnl_usd"] - r["open_pnl"], 3.00, places=2)   # closed cycle = +$3 net
        self.assertEqual(r["max_used"], 75)

    def test_emergency_stop_and_liquidation(self):
        bottom = dip_series([])[-1]["c"]
        path = [bottom * (1 - 0.01 * k) for k in range(1, 30)]           # keeps falling 29%
        with_stop = bt.run_dip(dip_series(path))
        self.assertEqual((with_stop["emergency"], with_stop["liquidated"]), (1, 0))
        self.assertTrue(-35 < with_stop["pnl_usd"] < -20)                 # ~-$27 with 5x (was ~-$55 with 10x)
        no_stop = bt.run_dip(dip_series(path), emergency=False)
        self.assertEqual(no_stop["liquidated"], 1)
        self.assertLess(no_stop["pnl_usd"], with_stop["pnl_usd"])

    def test_one_year_is_fast_and_report(self):
        m5 = walk(365 * 288, 300, vol=0.0025, seed=11)
        t = time.time()
        text = bt.report_text(m5)
        self.assertLess(time.time() - t, 60)
        for part in ("COMPRA NO FUNDO", "P/L TOTAL", "Resultado por mês", "Sem stop de emergência", "taxas e deslize dobrados"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
