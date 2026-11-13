import os
import tempfile
import time
import unittest
from unittest.mock import patch

import rocket_bot as rb
import signals as sg

NOW = time.time()
MINT = "ROCKcoin111111111111111111111111111111pump"


def pair(**kw):
    p = {"chainId": "solana", "dexId": "pumpfun", "marketCap": 60_000, "pairAddress": "POOL", "baseToken": {"address": MINT, "symbol": "RK"},
         "quoteToken": {"address": sg.SOL_MINT}, "liquidity": {"usd": 60_000}, "pairCreatedAt": (NOW - 3 * 3600) * 1000,
         "txns": {"h1": {"buys": 400, "sells": 150}, "m5": {"buys": 80, "sells": 20}}, "volume": {"h1": 60_000},
         "priceChange": {"m5": 15, "h1": 120}, "priceUsd": "0.0001"}
    p.update(kw)
    return p


class RocketTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_new_coin_exploding(self):
        self.assertEqual(rb.rocket_reason(pair(), NOW), "")

    def test_rejections(self):
        self.assertIn("velha", rb.rocket_reason(pair(pairCreatedAt=(NOW - 8 * 86400) * 1000), NOW))
        self.assertIn("nova demais", rb.rocket_reason(pair(pairCreatedAt=(NOW - 70 * 60) * 1000), NOW))
        self.assertIn("sem explosão", rb.rocket_reason(pair(priceChange={"m5": 1, "h1": 50}), NOW))
        self.assertIn("pico vertical", rb.rocket_reason(pair(priceChange={"m5": 95, "h1": 150}), NOW))
        self.assertIn("vermelho", rb.rocket_reason(pair(priceChange={"m5": 10, "h1": -20}), NOW))
        self.assertIn("pouca compra", rb.rocket_reason(pair(txns={"h1": {"buys": 400, "sells": 150},
                                                                  "m5": {"buys": 40, "sells": 35}}), NOW))
        self.assertIn("liquidez alta", rb.rocket_reason(pair(dexId="pumpswap", liquidity={"usd": 200_000}), NOW))

    def test_buys_once_per_coin(self):
        bought = []
        buyer = lambda p, s, c: bought.append(1) or ""
        rb.cycle(pairs=[pair()], now=NOW, safety=lambda m, curve=False: "", buyer=buyer)
        rb.cycle(pairs=[pair()], now=NOW + 45, safety=lambda m, curve=False: "", buyer=buyer)
        self.assertEqual(bought, [1])

    def test_pauses_after_5_losses(self):
        losses = [{"mode": "rocket", "pnl_usd": -4, "closed_at": time.time() + 5}] * 5
        with patch("live_trade_state.history", return_value=losses):
            self.assertEqual(rb.cycle(pairs=[pair()], now=NOW, safety=lambda m, curve=False: "",
                                      buyer=lambda p, s, c: ""), [])
        self.assertFalse(rb.enabled())

    def test_exit_rules(self):
        import live_exit_monitor as lem
        pos = {"mode": "rocket", "peak_ratio": 0, "opened_at": time.time()}
        self.assertEqual(lem.position_signal(pos, 10, 6.5)["action"], "hold")                 # -35%
        self.assertEqual(lem.position_signal(pos, 10, 1.0)["action"], "hold")                 # -90%: no stop loss
        self.assertEqual(lem.position_signal(dict(pos, peak_ratio=3.0), 10, 27)["action"], "hold")   # 3x, -10% from top
        self.assertEqual(lem.position_signal(dict(pos, peak_ratio=3.0), 10, 25)["action"], "sell")   # -17% from top
        old = dict(pos, opened_at=time.time() - 20 * 3600)
        self.assertEqual(lem.position_signal(old, 10, 8)["action"], "hold")                   # no time stop

    def test_take_profit_ladder(self):
        import live_trade_state as st
        positions = {MINT: {"mode": "rocket", "token_amount_raw": "1000", "cost_usd": 10}}
        sold, fields = [], {}
        with patch.object(st, "_load", return_value={"positions": positions}), \
             patch.object(st, "set_position_fields", side_effect=lambda m, **f: positions[m].update(f)):
            rb.take_profits(quote=lambda m, a: 15, seller=lambda *a: sold.append(a))      # 1.5x: nothing
            self.assertEqual(sold, [])
            rb.take_profits(quote=lambda m, a: 21, seller=lambda *a: sold.append(a))      # 2.1x: half
            self.assertEqual(sold[-1][1:3], (0.5, "rocket_tp_2x"))
            rb.take_profits(quote=lambda m, a: 25, seller=lambda *a: sold.append(a))      # no repeat
            self.assertEqual(len(sold), 1)
            rb.take_profits(quote=lambda m, a: 55, seller=lambda *a: sold.append(a))      # 5.5x: half of the rest
            self.assertEqual(sold[-1][1:3], (0.5, "rocket_tp_5x"))

    def test_rebuys_once_when_it_falls_20_percent(self):
        import live_trade_state as st
        positions = {MINT: {"mode": "rocket", "token_amount_raw": "1000", "cost_usd": 10}}
        bought = []
        with patch.object(st, "_load", return_value={"positions": positions}), \
             patch.object(st, "set_position_fields", side_effect=lambda m, **f: positions[m].update(f)):
            rb.take_profits(quote=lambda m, a: 8.5, dca_buyer=lambda *a: bought.append(1) or "")   # -15%: wait
            self.assertEqual(bought, [])
            rb.take_profits(quote=lambda m, a: 7.9, dca_buyer=lambda *a: bought.append(1) or "")   # -21%: rebuy
            rb.take_profits(quote=lambda m, a: 7.0, dca_buyer=lambda *a: bought.append(1) or "")   # only once
            self.assertEqual(bought, [1])
            self.assertTrue(positions[MINT]["dca_done"])

    def test_after_the_rebuy_it_closes_at_20_percent_over_the_average(self):
        import live_exit_monitor as lem
        pos = {"mode": "rocket", "dca_done": True, "dca_at": time.time(), "peak_ratio": 0}
        self.assertEqual(lem.position_signal(pos, 20, 23)["action"], "hold")             # +15%
        out = lem.position_signal(pos, 20, 24.2)                                         # +21%: close all
        self.assertEqual((out["action"], out["reason"]), ("sell", "rocket_dca_target"))
        self.assertEqual(lem.position_signal(pos, 20, 5)["action"], "hold")              # -75%: no stop loss

    def test_minimum_liquidity_30k_for_every_coin(self):
        curve = pair(dexId="pumpfun", liquidity={"usd": 5_000}, marketCap=60_000)
        self.assertIn("liquidez baixa", rb.rocket_reason(curve, NOW))        # 5k liquidity: never, even on the curve
        self.assertIn("liquidez baixa", rb.rocket_reason(dict(curve, liquidity={"usd": 0}), NOW))
        self.assertEqual(rb.rocket_reason(dict(curve, liquidity={"usd": 30_000}), NOW), "")

    def test_most_bought_first(self):
        less = pair(baseToken={"address": "A" * 40 + "pump", "symbol": "A"})
        more = pair(baseToken={"address": "B" * 40 + "pump", "symbol": "B"},
                    txns={"h1": {"buys": 900, "sells": 150}, "m5": {"buys": 200, "sells": 20}})
        order = []
        rb.cycle(pairs=[less, more], now=NOW, safety=lambda m, curve=False: "",
                 buyer=lambda p, s, c: order.append(p["baseToken"]["symbol"]) or "")
        self.assertEqual(order, ["B"])

    def test_closes_all_when_5_orders_together_fall_25_percent(self):
        import live_trade_state as st
        positions = {"M%d" % i: {"mode": "rocket", "token_amount_raw": "1000", "cost_usd": 10, "dca_done": True}
                     for i in range(5)}
        closed = []
        with patch.object(st, "_load", return_value={"positions": positions}), \
             patch.object(st, "positions_by_mode", return_value=5), \
             patch.object(st, "set_position_fields", side_effect=lambda m, **f: positions[m].update(f)):
            rb.take_profits(quote=lambda m, a: 8, closer=closed.append)        # all -20%: keep
            self.assertEqual(closed, [])
            rb.take_profits(quote=lambda m, a: 7.4, closer=closed.append)      # all -26%: close all
            self.assertEqual(sorted(closed), sorted(positions))
        with patch.object(st, "_load", return_value={"positions": dict(list(positions.items())[:3])}), \
             patch.object(st, "positions_by_mode", return_value=3):
            closed.clear()
            rb.take_profits(quote=lambda m, a: 5, closer=closed.append)        # only 3 open: no basket stop
            self.assertEqual(closed, [])

    def test_status(self):
        self.assertIn("SNIPER", rb.status_text())


if __name__ == "__main__":
    unittest.main()
