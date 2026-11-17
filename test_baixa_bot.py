import unittest
from unittest import mock

import baixa_bot as bb

H = 3600


def candles(prices):
    return [{"t": i * H, "h": p, "l": p, "c": p} for i, p in enumerate(prices)]


class BaixaTest(unittest.TestCase):
    def setUp(self):
        bb._MEM.clear()

    def test_signal_and_exit(self):
        ok, px, top = bb.signal(candles([10] * 168 + [9.4]))
        self.assertTrue(ok)                                       # -6% from the 7-day top
        self.assertFalse(bb.signal(candles([10] * 168 + [9.6]))[0])
        self.assertFalse(bb.signal(candles([10] * 50 + [5]))[0])  # not enough history: no buy
        self.assertEqual(bb.exit_signal(1.26), ("sell", "take_profit"))
        self.assertEqual(bb.exit_signal(0.59), ("sell", "stop_loss"))
        self.assertEqual(bb.exit_signal(1.05)[0], "hold")
        self.assertEqual(bb.exit_signal(1.08), ("sell", "take_profit"))     # owner: +8%
        self.assertEqual(bb.exit_signal(0.80), ("sell", "stop_loss"))       # owner: -20%

    def test_cycle_buys_only_when_on_and_with_sol(self):
        bb._load()["tokens"] = {"BP": {"mint": "MintBP", "pool": "P1", "liq": 900000}}
        bb._load()["resolved_at"] = 1e12
        bought = []
        buy = lambda pair, mint, sym, send, chat: bought.append(sym) or True
        with mock.patch("solid_dip_bt.gecko_hours", return_value=candles([10] * 168 + [9.0])), \
                mock.patch.object(bb, "_open", return_value=set()), mock.patch.object(bb, "day_pnl", return_value=0):
            self.assertEqual(bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150), [])   # off
            bb.set_enabled(True)
            self.assertEqual(bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: False, sol_usd=150), [])  # no SOL
            self.assertEqual(bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150), ["BP"])
        with mock.patch("solid_dip_bt.gecko_hours", return_value=candles([10] * 168 + [9.0])), \
                mock.patch.object(bb, "_open", return_value={"MintBP"}), mock.patch.object(bb, "day_pnl", return_value=0):
            self.assertEqual(bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150), [])   # one per coin
        self.assertIn("Backtest do robô", bb.status_text())

    def test_exit_monitor_uses_the_rule(self):
        import live_exit_monitor as lem
        pos = {"mode": "baixa", "cost_usd": 10, "peak_ratio": 1.0}
        self.assertEqual(lem.position_signal(pos, 10, 12.6)["reason"], "take_profit")
        self.assertEqual(lem.position_signal(pos, 10, 5.9)["reason"], "stop_loss")
        self.assertEqual(lem.position_signal(pos, 10, 10.5)["action"], "hold")


class BaixaBacktestTest(unittest.TestCase):
    def setUp(self):
        bb._MEM.clear()

    def test_backtest_uses_robot_rule(self):
        bb._load()["tokens"] = {"BP": {"mint": "M", "pool": "P", "liq": 1e6}}
        prices = ([10] * 170 + [9.4] + [9.6] * 5 + [10.2] + [10] * 200) * 3        # dips and +8% recoveries
        cs = [{"t": 1_700_000_000 + i * H, "h": p, "l": p, "c": p} for i, p in enumerate(prices)]
        with mock.patch("solid_dip_bt.gecko_hours", return_value=cs):
            rep = bb.backtest(now=1_800_000_000)
        self.assertEqual(rep["coins"], ["BP"])
        self.assertGreater(rep["full"]["wins"], 0)
        txt = bb.backtest_text(rep)
        self.assertIn("alvo +8%", txt)
        self.assertIn("stop −20%", txt)


if __name__ == "__main__":
    unittest.main()
