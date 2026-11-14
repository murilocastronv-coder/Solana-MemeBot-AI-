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

    def test_does_not_pause_after_losses(self):
        losses = [{"mode": "rocket", "pnl_usd": -4, "closed_at": time.time() + 5}] * 8
        with patch("live_trade_state.history", return_value=losses):
            rb.cycle(pairs=[pair()], now=NOW, safety=lambda m, curve=False: "", buyer=lambda p, s, c: "")
        self.assertTrue(rb.enabled())

    def test_exit_rules(self):
        import live_exit_monitor as lem
        pos = {"mode": "rocket", "peak_ratio": 0, "opened_at": time.time()}
        self.assertEqual(lem.position_signal(pos, 10, 7.2)["action"], "hold")                 # -28%
        self.assertEqual(lem.position_signal(pos, 10, 6.9)["reason"], "stop_loss")            # -31%: fixed stop 30%
        self.assertEqual(lem.position_signal(dict(pos, peak_ratio=3.0), 10, 27.5)["action"], "hold")  # 3x, -8% from top
        self.assertEqual(lem.position_signal(dict(pos, peak_ratio=3.0), 10, 26.5)["action"], "sell")  # -12% from top
        old = dict(pos, opened_at=time.time() - 61 * 60)
        self.assertEqual(lem.position_signal(old, 10, 9)["reason"], "time_stop")              # 60 min, never +15%
        self.assertEqual(lem.position_signal(dict(old, peak_ratio=1.2), 10, 11.5)["action"], "hold")  # moved: keep

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

    def test_minimum_liquidity_30k_for_every_coin(self):
        curve = pair(dexId="pumpfun", liquidity={"usd": 5_000}, marketCap=60_000)
        self.assertIn("liquidez baixa", rb.rocket_reason(curve, NOW))        # 5k liquidity: never, even on the curve
        self.assertIn("liquidez baixa", rb.rocket_reason(dict(curve, liquidity={"usd": 0}), NOW))
        self.assertEqual(rb.rocket_reason(dict(curve, liquidity={"usd": 15_000}), NOW), "")
        self.assertIn("liquidez baixa", rb.rocket_reason(dict(curve, liquidity={"usd": 14_000}), NOW))

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
        with patch.object(st, "_load", return_value={"positions": dict(list(positions.items())[:2])}), \
             patch.object(st, "positions_by_mode", return_value=2):
            closed.clear()
            rb.take_profits(quote=lambda m, a: 7.4, closer=closed.append)      # only 2 open, -26%: no basket stop
            self.assertEqual(closed, [])

    def test_no_rebuy_and_fixed_50_percent_stop_closes_only_that_coin(self):
        import live_trade_state as st
        positions = {"A": {"mode": "rocket", "token_amount_raw": "1", "cost_usd": 10},
                     "B": {"mode": "rocket", "token_amount_raw": "1", "cost_usd": 10}}
        bought, closed = [], []
        with patch.object(st, "_load", return_value={"positions": positions}), \
             patch.object(st, "set_position_fields", side_effect=lambda m, **f: positions[m].update(f)):
            rb.take_profits(quote=lambda m, a: 4.0 if m == "A" else 12.0, closer=closed.append,
                            dca_buyer=lambda *a: bought.append(1) or "")
        self.assertEqual((bought, closed), ([], []))     # never averages down, never closes everything
        import live_exit_monitor as lem
        self.assertEqual(lem.position_signal(positions["A"], 10, 4.0)["reason"], "stop_loss")   # A alone is sold

    def test_profit_protection_after_15_percent(self):
        import live_exit_monitor as lem
        pos = {"mode": "rocket", "peak_ratio": 1.18, "opened_at": time.time()}
        self.assertEqual(lem.position_signal(pos, 10, 11.0)["action"], "hold")            # +10% after +18%: keep
        out = lem.position_signal(pos, 10, 10.5)                                           # +5% after +18%: out
        self.assertEqual(out["action"], "sell")                                            # trailing 10% under top
        low = {"mode": "rocket", "peak_ratio": 1.10, "opened_at": time.time()}
        self.assertEqual(lem.position_signal(low, 10, 8.0)["action"], "hold")             # never reached +15%: -20% ok

    def test_status(self):
        self.assertIn("SNIPER", rb.status_text())


if __name__ == "__main__":
    unittest.main()


class AnyDexTests(unittest.TestCase):
    def test_coins_from_other_dexes_on_dexscreener(self):
        ray = pair(dexId="raydium", baseToken={"address": "RAYcoin11111111111111111111111111111111", "symbol": "R"})
        self.assertEqual(rb.rocket_reason(ray, NOW), "")
        self.assertIn("SOL/USDC", rb.rocket_reason(dict(ray, quoteToken={"address": "OTHER"}), NOW))
        self.assertIn("Solana", rb.rocket_reason(dict(ray, chainId="base"), NOW))


class SignalTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_signal_message_has_contract_entry_sl_and_tps_to_copy(self):
        txt = rb.signal_text(pair(priceUsd="0.0001"))
        self.assertIn("`" + MINT + "`", txt)
        self.assertIn("Entrada: `0.0001`", txt)
        self.assertIn("Stop loss (−30%): `0.00007`", txt)
        self.assertIn("TP2 (2x): `0.0002`", txt)
        self.assertIn("TP3 (5x): `0.0005`", txt)

    def test_signals_without_buying(self):
        rb.set_enabled(False)
        rb.set_signals(True)
        sent, bought = [], []
        rb.cycle(lambda c, t: sent.append(t), 1, pairs=[pair()], now=NOW, safety=lambda m, curve=False: "",
                 buyer=lambda p, s, c: bought.append(1) or "")
        self.assertEqual(bought, [])
        self.assertTrue(any("SINAL SNIPER" in t for t in sent))

    def test_scam_gets_no_signal(self):
        rb.set_signals(True)
        sent = []
        rb.cycle(lambda c, t: sent.append(t), 1, pairs=[pair()], now=NOW, safety=lambda m, curve=False: "freeze ativo")
        self.assertFalse(any("SINAL SNIPER" in t for t in sent))


class CopySpanTests(unittest.TestCase):
    def test_backticks_become_tap_to_copy(self):
        import telegram_bot as tb
        text, ents = tb.code_spans("💵 Entrada: `0.0001`\nSL: `0.00007`")
        self.assertEqual(text, "💵 Entrada: 0.0001\nSL: 0.00007")
        self.assertEqual(len(ents), 2)
        first = ents[0]
        self.assertEqual(text.encode("utf-16-le")[first["offset"] * 2:(first["offset"] + first["length"]) * 2].decode("utf-16-le"),
                         "0.0001")


class GrowthPlanTest(unittest.TestCase):
    def setUp(self):
        import tempfile, pathlib
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        path = pathlib.Path(self.tmp.name) / "rocket_bot.json"
        self.p = mock.patch.object(rb, "_path", return_value=path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_locked_until_proven(self):
        few = [{"pnl_usd": 5}] * 10
        self.assertEqual(rb.order_size(500, trades=few)[0], rb.ORDER_USD)
        losing = [{"pnl_usd": -1}] * 60
        self.assertEqual(rb.order_size(500, trades=losing)[0], rb.ORDER_USD)

    def test_tiers_get_more_conservative(self):
        good = [{"pnl_usd": 1}] * 60
        self.assertAlmostEqual(rb.order_size(500, trades=good)[0], 40)        # 8%
        self.assertAlmostEqual(rb.order_size(5_000, trades=good)[0], 250)     # 5%
        self.assertLess(rb.growth_pct(200_000), rb.growth_pct(50_000))
        self.assertEqual(rb.order_size(100, trades=good)[0], rb.ORDER_USD)    # never under the base size

    def test_drawdown_halves_and_liquidity_caps(self):
        good = [{"pnl_usd": 1}] * 60
        rb.order_size(5_000, trades=good)
        self.assertAlmostEqual(rb.order_size(3_000, trades=good)[0], 75)      # 40% under top: 5% / 2
        self.assertAlmostEqual(rb.order_size(3_000, liquidity=2_000, trades=good)[0], 20)   # 1% of the pool

    def test_month_profit_is_locked(self):
        jan = 1767225600   # 2026-01-01
        rb.track_account(1_000, now=jan)
        g = rb.track_account(2_000, now=jan + 32 * 86400)
        self.assertAlmostEqual(g["locked"], 200)
        self.assertEqual(g["months"], {"2026-01": 1000})
