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
        self.assertEqual(bb.exit_signal(1.10)[0], "hold")

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
        self.assertIn("PERDEU fora da amostra", bb.status_text())

    def test_exit_monitor_uses_the_rule(self):
        import live_exit_monitor as lem
        pos = {"mode": "baixa", "cost_usd": 10, "peak_ratio": 1.0}
        self.assertEqual(lem.position_signal(pos, 10, 12.6)["reason"], "take_profit")
        self.assertEqual(lem.position_signal(pos, 10, 5.9)["reason"], "stop_loss")
        self.assertEqual(lem.position_signal(pos, 10, 11.0)["action"], "hold")


if __name__ == "__main__":
    unittest.main()
