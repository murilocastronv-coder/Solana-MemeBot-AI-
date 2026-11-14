import pathlib
import tempfile
import time
import unittest
from unittest import mock

import rocket_bot as rb
import sniper_sim as ss

NOW = 1_800_000_000


def pair(mint="M1", price="1.0"):
    return {"baseToken": {"address": mint, "symbol": mint}, "priceUsd": price, "priceChange": {"m5": 10}}


class SniperSimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(rb, "_path", return_value=pathlib.Path(self.tmp.name) / "r.json")
        self.p.start()
        ss.to_simulation_once()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_switch_once_and_paper(self):
        self.assertTrue(ss.paper())
        self.assertFalse(ss.to_simulation_once())

    def test_buy_respects_max_open_and_cash(self):
        for i in range(rb.MAX_OPEN):
            self.assertEqual(ss.buy(pair("M" + str(i)), now=NOW), "")
        self.assertIn("abertas", ss.buy(pair("MX"), now=NOW))
        self.assertAlmostEqual(rb._load()["sim"]["cash"], ss.START_USD - rb.MAX_OPEN * rb.ORDER_USD)

    def test_stop_loss_and_trailing(self):
        ss.buy(pair("A"), now=NOW)
        ss.buy(pair("B"), now=NOW)
        closed = ss.exits({"A": 0.6, "B": 1.25}, now=NOW + 60)          # A -40%: fixed stop
        self.assertEqual([c["symbol"] for c in closed], ["A"])
        self.assertLess(closed[0]["pnl"], -0.35 * rb.ORDER_USD)
        closed = ss.exits({"B": 1.05}, now=NOW + 120)                  # B fell from +22% top
        self.assertEqual(len(closed), 1)
        self.assertEqual(rb._load()["sim"]["positions"], {})

    def test_time_stop_when_it_never_moves(self):
        ss.buy(pair("A"), now=NOW)
        with mock.patch("time.time", return_value=NOW + rb.NO_MOVE_S + 60):
            closed = ss.exits({"A": 1.02}, now=NOW + rb.NO_MOVE_S + 60)
        self.assertEqual(closed[0]["reason"], "time_stop")

    def test_ladder_sells_half_at_2x(self):
        ss.buy(pair("A"), now=NOW)
        ss.exits({"A": 2.2}, now=NOW + 60)
        pos = rb._load()["sim"]["positions"]["A"]
        self.assertAlmostEqual(pos["qty"], pos["qty0"] / 2)

    def test_cycle_buys_virtually_not_real(self):
        with mock.patch.object(rb, "rocket_reason", return_value=""), \
                mock.patch.object(rb, "_buy", side_effect=AssertionError("real buy")):
            got = rb.cycle(pairs=[pair("A")], now=NOW, safety=lambda m, curve=False: "")
        self.assertEqual(got, ["A"])
        self.assertIn("A", rb._load()["sim"]["positions"])


if __name__ == "__main__":
    unittest.main()
