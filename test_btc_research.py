import random
import unittest

import btc_research as r


def market(n=6000, drift=0.0, seed=1):
    random.seed(seed)
    cs, p, t = [], 100_000.0, 0
    for i in range(n):
        o = p
        p *= 1 + drift + random.gauss(0, 0.0008)
        v = random.expovariate(1) * 10
        cs.append((t, o, max(o, p) * 1.0003, min(o, p) * 0.9997, p, v, v * random.uniform(0.3, 0.7)))
        t += 60
    return cs


class ResearchTests(unittest.TestCase):
    def test_features_and_simulation(self):
        cs = market()
        f = r.features(cs)
        p = {k: v[0] for k, v in r.GRID.items()}
        p["arm"], p["drop"] = p.pop("exitp")
        trades = r.simulate(cs, f, p)
        self.assertIsInstance(trades, list)
        self.assertTrue(all(t >= -0.3 - 1e-9 for t in trades))       # never below the stop

    def test_search_uses_unseen_test_period(self):
        rows, split = r.search(market(9000), min_trades=1)
        self.assertGreater(split, 0)
        for row in rows:
            self.assertIn("test", row)
        self.assertIn("PESQUISA", r.report(rows) if rows else "PESQUISA")


if __name__ == "__main__":
    unittest.main()
