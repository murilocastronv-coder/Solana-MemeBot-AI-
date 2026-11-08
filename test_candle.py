import time
import unittest
from unittest.mock import patch

import live_engine as e

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
MINT2 = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"


def hype(mint=MINT, vol5=50_000, **over):
    p = {"baseToken": {"address": mint, "symbol": "HYPE"}, "priceUsd": "0.01",
         "liquidity": {"usd": 500_000}, "fdv": 5_000_000,
         "volume": {"h1": 800_000, "m5": vol5},
         "txns": {"m5": {"buys": 200, "sells": 150}},
         "pairCreatedAt": (time.time() - 86400) * 1000}
    p.update(over)
    return p


class CandleTests(unittest.TestCase):
    def test_pick_hottest_first_and_filters(self):
        rows = [hype(vol5=20_000), hype(MINT2, vol5=90_000), hype(liquidity={"usd": 30_000})]
        picked = e.candle_pick(rows)
        self.assertEqual([p["baseToken"]["address"] for p in picked], [MINT2, MINT])

    def _open(self, cost):
        bought = []
        with patch.object(e, "ready", return_value=True), \
             patch.object(e.state, "positions_by_mode", return_value=0), \
             patch.object(e.state, "can_open", return_value=(True, "Ready")), \
             patch.object(e, "get_address", return_value="W"), \
             patch.object(e, "order_size_usd", return_value=(10.0, 200.0)), \
             patch.object(e.state, "_load", return_value={"positions": {}}), \
             patch.object(e, "token_safety", return_value=(True, "")), \
             patch.object(e, "candle_roundtrip_cost", return_value=cost), \
             patch.object(e, "_execute_buy", side_effect=lambda *a: bought.append(a) or {"status": "settled_buy"}):
            e.candle_open([hype()], lambda *a: None, "1")
        return bought

    def test_buys_when_cost_is_small(self):
        bought = self._open(0.004)
        self.assertEqual(len(bought), 1)
        self.assertEqual(bought[0][-1], "candle")
        self.assertEqual(bought[0][-2], e.CANDLE_FRACTION)

    def test_skips_expensive_round_trip(self):
        self.assertEqual(self._open(0.03), [])
        self.assertIn("custo de ida e volta", e.CANDLE_SCAN["why"])

    def test_close_sells_only_candle_positions(self):
        sold = []
        positions = {MINT: {"mode": "candle"}, MINT2: {"mode": "hot"}}
        with patch.object(e.state, "_load", return_value={"positions": positions}), \
             patch.object(e, "sell_now", side_effect=lambda m, r, s, c: sold.append((m, r))):
            e.candle_close(lambda *a: None, "1")
        self.assertEqual(sold, [(MINT, "candle_close")])


if __name__ == "__main__":
    unittest.main()
