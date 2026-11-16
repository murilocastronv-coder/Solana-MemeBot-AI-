import unittest

import snipe_backtest as sbt
import sniper_pro as sp

P = {"ages": [10], "min_vol": 2000, "min_ratio": 0, "min_up": -100, "max_up": 10000, "min_score": 0, "tp": 0.20, "sl": 0.08,
     "trail": 0, "hold": 30, "partial": False, "size": 10, "max_open": 3, "reserve": 30, "daily_loss": 5, "bucket": 0}
SNAP = {"price": 1e-5, "mcap": 20_000, "vol5": 5_000, "buys": 50, "sells": 20, "up5": 5}


class WalkTest(unittest.TestCase):
    def test_exit_at_real_price_not_at_level(self):
        path = [[0, 1.0], [10, 1.05], [20, 1.35]]                  # jumps past +20%: sells at +35% (real trade)
        g, why, t, sells = sp.walk(path, P)
        self.assertEqual((round(g, 2), why, t), (1.35, "take_profit", 20))
        g, why, _, _ = sp.walk([[0, 1.0], [10, 0.70]], P)          # gap below the stop: sells at -30%, not -8%
        self.assertEqual((round(g, 2), why), (0.70, "stop_loss"))
        self.assertEqual(sp.walk([], P)[0], 0.0)                   # no trade after the entry = total loss

    def test_partial_trailing_failure_delay(self):
        g, why, _, sells = sp.walk([[0, 1.0], [10, 1.25], [20, 0.99]], dict(P, partial=True))
        self.assertAlmostEqual(g, 0.5 * 1.25 + 0.5 * 0.99)
        self.assertEqual(sells, 2)
        g, why, _, _ = sp.walk([[0, 1.0], [10, 1.5], [20, 1.30]], dict(P, trail=0.10))
        self.assertEqual((round(g, 2), why), (1.30, "stop móvel"))
        g, why, _, _ = sp.walk([[0, 1.0], [10, 1.25], [20, 1.10]], P, fail=1.0)   # the sale fails: goes at the next trade
        self.assertAlmostEqual(g, 1.10)
        g, _, _, _ = sp.walk([[0, 1.0], [10, 1.1], [20, 1.4]], P, delay_steps=1)  # one trade later: worse entry
        self.assertAlmostEqual(g, 1.4 / 1.1)

    def test_no_look_ahead(self):
        trades = [(100, 1.0, 1000, "buy"), (200, 1.1, 1500, "buy"), (400, 9.9, 99999, "buy")]
        snap = sbt.snapshot(trades, 300)                           # decision at t=300: the trade at 400 is not seen
        self.assertEqual(snap["price"], 1.1)
        self.assertEqual(snap["vol5"], 2500)
        c = {"coins": {"m": {"created": 0, "snaps": {"10": SNAP}, "after": {"10": [[600 + sbt.DELAY_S, 1.0], [700, 1.3]]}}}}
        cands = sp.candidates(c, 0, 10, P)
        self.assertGreaterEqual(c["coins"]["m"]["after"]["10"][0][0], cands[0][0])   # entry after the decision moment


class PortfolioTest(unittest.TestCase):
    def coins(self, n, ratio):
        return {"coins": {"m" + str(i): {"created": i * 100, "snaps": {"10": SNAP},
                                         "after": {"10": [[i * 100 + 620, 1.0], [i * 100 + 700, ratio]]}} for i in range(n)}}

    def test_cash_reserve_fees(self):
        s = sp.simulate(self.coins(1, 1.30), 0, 10_000, P)
        exp = 10 * 1.30 * 0.97 * 0.97 - 10 - 2 * sp.NET_FEE
        self.assertAlmostEqual(s["pnl"], round(exp, 2), places=2)
        self.assertAlmostEqual(s["final"], round(60 + exp, 2), places=2)
        s = sp.simulate(self.coins(40, 0.5), 0, 10_000, dict(P, daily_loss=0))
        self.assertGreaterEqual(s["final"], 30 - 10)               # never below the reserve minus one open trade
        self.assertIn("sem caixa acima da reserva", s["skipped"])
        self.assertGreater(s["fees"], 0)

    def test_optimise_and_text(self):
        import random
        c = {"points": {}, "coins": {}}
        end = 1_800_000_000
        rnd = random.Random(3)
        for i in range(600):
            created = end - 180 * 86400 + i * 25000
            ratio = 1.4 if rnd.random() < 0.6 else 0.85
            c["coins"]["m" + str(i)] = {"created": created, "snaps": {"10": SNAP, "15": SNAP},
                                        "after": {"10": [[created + 620, 1.0], [created + 900, ratio]],
                                                  "15": [[created + 920, 1.0], [created + 1200, ratio]]}}
        o = sp.optimise(c, end, "B", n=60)
        self.assertGreater(o["test"]["trades"], 0)
        o["robust"] = sp.robustness(c, end, o["params"])
        rep = {"data": sp.data_check(c, end), "profiles": {"B": o}, "ages": {}, "winner": "B" if sp.validated(o, o["robust"]) else None}
        txt = sp.text(rep)
        self.assertIn("TESTE INDEPENDENTE", txt)
        self.assertIn("holders", txt)                              # missing data is reported, not invented


if __name__ == "__main__":
    unittest.main()
