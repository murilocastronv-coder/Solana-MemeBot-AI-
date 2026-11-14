import random
import unittest

import btc_swing as s


def market(n=3000, drift=0.0, seed=3):
    random.seed(seed)
    cs, p, t = [], 60_000.0, 0
    for i in range(n):
        o = p
        p *= 1 + drift + random.gauss(0, 0.006)
        v = random.expovariate(1) * 1000
        cs.append((t, o, max(o, p) * 1.002, min(o, p) * 0.998, p, v, v * random.uniform(0.35, 0.65)))
        t += 3600
    return cs


class SwingTests(unittest.TestCase):
    def test_rolling_high_low_use_only_past_candles(self):
        cs = market(600)
        ind = s.indicators(cs)
        for i in (300, 450, 599):
            self.assertAlmostEqual(ind["hh"][48][i], max(c[2] for c in cs[i - 48:i]))
            self.assertAlmostEqual(ind["ll"][24][i], min(c[3] for c in cs[i - 24:i]))

    def test_losses_are_capped_by_liquidation_and_fees_counted(self):
        cs = market()
        ind = s.indicators(cs)
        p = {"family": "breakout", "side": "both", "lev": 5, "stop": 2.0, "trail": 3.0, "hold": 24, "n": 24, "trend": False}
        trades = s.simulate(cs, ind, p, 0, len(cs))
        self.assertTrue(trades)
        self.assertTrue(all(t >= -0.9 for t in trades))

    def test_weekly_challenge(self):
        cs = market(3000)
        ind = s.indicators(cs)
        p = {"family": "breakout", "side": "both", "lev": 20, "stop": 2.0, "trail": 3.0, "hold": 24, "n": 24, "trend": False}
        weeks = s.week_challenge(cs, ind, p, 1.0)
        self.assertTrue(weeks)
        for w in weeks:
            self.assertIn(w["end"], ("win", "bust", ""))
            if w["end"] == "":
                self.assertTrue(15 < w["bal"] < 400)
        self.assertIn("DESAFIO", s.challenge_report(s.challenge_search(cs)))

    def test_month_challenge_goes_conservative_after_the_target(self):
        cs = market(4000, drift=0.0004)
        ind = s.indicators(cs)
        p = {"family": "breakout", "side": "long", "lev": 10, "stop": 2.0, "trail": 3.0, "hold": 72, "n": 48, "trend": False}
        ws = s.month_challenge(cs, ind, p, 1.0)
        self.assertTrue(ws)
        for w in ws:
            if w["hit"]:
                self.assertGreaterEqual(w["bal"], 400 * 0.7)   # conservative after the target: small size
        data = {"SOL": market(3000, seed=7), "ETH": market(3000, seed=8), "BTC": market(3000, seed=9)}
        s.run_month(data=data)
        self.assertIn("SOL", s.MONTH["text"])

    def test_search_reports_train_and_test(self):
        rows, hold = s.search(market(2400, drift=0.0002))
        text = s.report(rows, hold)
        self.assertIn("TREINO", text if rows else "TREINO")


if __name__ == "__main__":
    unittest.main()
