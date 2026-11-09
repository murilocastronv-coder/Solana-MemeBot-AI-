import math
import unittest

from btc_mac_7_10 import rule, sim
from btc_mac_7_10.data import chain

T0 = 1_546_300_800  # 2019-01-01


def candles(prices):
    return [{"t": T0 + i * 300, "o": prices[i - 1] if i else p, "h": p, "l": p, "c": p} for i, p in enumerate(prices)]


class RuleTests(unittest.TestCase):
    def test_contrarian_band_and_hold(self):
        p = [100.0] * 12 + [90.0] * 3          # sharp drop: SMA7 < SMA10 by > 1% -> LONG (contrarian)
        s = rule.signals(p)
        self.assertEqual(s[13], 0)              # -0.87%: still inside the band
        self.assertEqual(s[14], 1)              # -1.33%: LONG
        up = p + [110.0] * 6                    # sharp rise: SMA7 > SMA10 by > 1% -> SHORT
        self.assertEqual(rule.signals(up)[-1], -1)
        flat = up + [up[-1]] * 30               # inside the band: keep the last signal
        self.assertEqual(rule.signals(flat)[-1], rule.signals(flat)[-20])

    def test_no_lookahead(self):
        p = [100 + math.sin(i / 3) * 3 for i in range(300)]
        self.assertEqual(rule.signals(p)[:200], rule.signals(p[:200]))

    def test_original_formula_counts_entry_and_flips(self):
        p = [100.0] * 12 + [90.0] * 5 + [110.0] * 6
        r = rule.original_backtest(p)
        self.assertEqual(r["trades"], 2)


class SimTests(unittest.TestCase):
    def test_controlled_loss_at_stop_is_about_2_percent(self):
        p = [100.0] * 12 + [90.0] * 3 + [90.0 * 0.8] * 3   # long signal, then -20%
        res = sim.run(candles(p), sim.SimConfig(mode="controlado", leverage=2.0))
        t = res["trades"][0]
        self.assertEqual(t["reason"], "stop")
        self.assertLess(-t["pnl"] / t["equity_before"], 0.035)  # gap through the stop costs a bit more

    def test_original_2x_long_gets_liquidated_on_big_drop(self):
        p = [100.0] * 12 + [90.0] * 3 + [40.0] * 3
        res = sim.run(candles(p), sim.SimConfig(mode="original", leverage=2.0))
        self.assertEqual(res["liquidations"], 1)

    def test_long_only_never_shorts_and_leverage_capped(self):
        p = [100.0] * 12 + [110.0] * 4 + [90.0] * 6
        res = sim.run(candles(p), sim.SimConfig(mode="original", long_only=True, leverage=5))
        self.assertTrue(all(t["side"] == 1 for t in res["trades"]))
        self.assertEqual(sim.SimConfig(leverage=5).lev, 2.0)

    def test_daily_limit_blocks_new_entries(self):
        p = [100.0] * 12
        for _ in range(6):                       # repeated crash -> long -> stop
            p += [p[-1] * 0.9] * 3 + [p[-1] * 0.9 * 0.85] * 14
        res = sim.run(candles(p), sim.SimConfig(mode="controlado", leverage=2.0, daily_loss_limit=0.03))
        self.assertGreaterEqual(res["limit_hits"], 1)
        self.assertLess(len(res["trades"]), 6)

    def test_goal_windows_and_verdict(self):
        k = math.log(11) / (30 * 288)           # 11x every 30 days
        curve = [(T0 + i * 300, 50.0, 50.0 * math.exp(k * i)) for i in range(288 * 40)]
        g = sim.goal_windows(curve, 10.0)
        self.assertEqual(g["atingiu"], g["janelas"])
        self.assertFalse(sim.verdict({"operacoes": 10, "fator_lucro": 0.9, "retorno_liquido_pct": 5,
                                      "perdeu_tudo": False, "liquidacoes": 0})["passou"])

    def test_chain_keeps_continuity(self):
        y = {"2019": candles([1.0, 1.1, 1.2]), "2020": candles([1.0, 0.5])}
        c = chain(y)
        self.assertAlmostEqual(c[3]["c"], 1.2)
        self.assertAlmostEqual(c[4]["c"], 0.6)


if __name__ == "__main__":
    unittest.main()
