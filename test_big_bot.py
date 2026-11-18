import unittest
from unittest import mock

import big_bot as bb
import trade_panel as tp

JUP = bb.MAJORS["JUP"]
ZEC = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"


def get_factory(prices, highs, jup_rows=None):
    def get(url):
        if "lite-api.jup.ag" in url:
            return jup_rows or []
        if "klines" in url:
            sym = url.split("symbol=")[1].split("&")[0]
            return [[0, 0, h, 0, 0] for h in highs.get(sym, [])]
        return [{"baseToken": {"address": m}, "priceUsd": str(px), "liquidity": {"usd": 1e6}, "priceChange": {}}
                for m, px in prices.items()]
    return get


class BigBotTest(unittest.TestCase):
    def setUp(self):
        bb._MEM.clear()
        bb._CHG.clear()
        tp._MEM.clear()
        self.p = [mock.patch.object(bb, "_save"), mock.patch.object(tp, "_save")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_zec_only_when_verified_on_jupiter(self):
        rows = [{"id": "FAKE", "symbol": "ZEC", "isVerified": False, "liquidity": 9e9},
                {"id": ZEC, "symbol": "ZEC", "isVerified": True, "liquidity": 1e6}]
        cs = bb.coins(get=lambda url: rows, now=1000)
        self.assertEqual(cs["ZEC"], ZEC)
        self.assertEqual(cs["JUP"], JUP)
        bb._MEM.clear()
        self.assertNotIn("ZEC", bb.coins(get=lambda url: rows[:1], now=1000))

    def test_signal_5_percent_below_binance_top(self):
        get = get_factory({JUP: 0.94}, {"JUPUSDT": [1.0] * 168})
        with mock.patch.object(bb, "coins", return_value={"JUP": JUP}):
            sigs = bb.signals_now(get, now=10_000)
        self.assertEqual(sigs[0][:2], ("JUP", JUP))
        self.assertTrue(sigs[0][4])
        bb._MEM.clear()
        with mock.patch.object(bb, "coins", return_value={"JUP": JUP}):
            self.assertFalse(bb.signals_now(get_factory({JUP: 0.97}, {"JUPUSDT": [1.0] * 168}), now=10_000)[0][4])

    def test_robot_buys_with_rules_and_respects_limits(self):
        positions = {}
        bought = []

        def buy(chat, mint, sol, send, sym):
            bought.append((mint, sol))
            positions[mint] = {"mode": "trade"}
        with mock.patch.object(tp, "is_owner", return_value=True), \
                mock.patch.object(tp, "positions", side_effect=lambda c: list(positions.items())), \
                mock.patch.object(bb, "open_positions", side_effect=lambda c: list(positions)), \
                mock.patch.object(bb, "apply_rules") as rules:
            tp._load()["robot"] = {"on": True, "sol": 0.2, "bought": {}}
            sigs = [("A", "MA", 0.9, 1.0, True), ("B", "MB", 0.9, 1.0, True), ("C", "MC", 0.9, 1.0, True), ("D", "MD", 1.0, 1.0, False)]
            done = bb.run_for(1, sigs, lambda *a, **k: None, now=5000, buy=buy)
            self.assertEqual(done, ["A", "B", "C"])                 # up to 6 open (only 3 in the dip)
            self.assertEqual(rules.call_count, 3)
            positions.clear()
            self.assertEqual(bb.run_for(1, sigs[:1], lambda *a, **k: None, now=6000, buy=buy), [])   # 24 h cooldown
        self.assertEqual(bought, [("MA", 0.2), ("MB", 0.2), ("MC", 0.2)])

    def test_exit_rules_match_the_strategy(self):
        import user_trade as ut
        self.assertEqual(bb.SL, 0)                                  # owner (Nov 18): no stop loss
        self.assertEqual(ut.exit_reason({"sl": bb.SL, "trail": bb.TRAIL}, 0.2, 1.0), None)
        p = {"sl": 0.35, "trail": bb.TRAIL}
        self.assertEqual(ut.exit_reason(p, 0.64, 1.0), "sl")
        self.assertIsNone(ut.exit_reason(p, 0.70, 1.0))
        self.assertEqual(ut.exit_reason(p, 1.0, 1.12), "trail")    # armed at +12%, 10% below the top
        self.assertIsNone(ut.exit_reason(p, 1.05, 1.08))           # never reached +10%: no trailing
        with mock.patch.object(tp, "_load", return_value={"tp": 0, "sl": 0, "trail": 0}):
            self.assertEqual(tp.exit_signal({"trade_sl": 0.35, "trade_trail": bb.TRAIL}, 0.6, 1.0), ("sell", "trade_sl"))
            self.assertEqual(tp.exit_signal({"trade_sl": bb.SL, "trade_trail": bb.TRAIL}, 0.2, 1.0)[0], "hold")
            self.assertEqual(tp.exit_signal({"trade_sl": bb.SL, "trade_trail": bb.TRAIL}, 1.0, 1.12), ("sell", "trade_trail"))


if __name__ == "__main__":
    unittest.main()


class BacktestTest(unittest.TestCase):
    def cs(self, prices, t0=0):
        return [{"t": t0 + i * 3600, "h": p, "l": p, "c": p} for i, p in enumerate(prices)]

    def test_buys_dip_and_sells_on_trailing(self):
        prices = [10.0] * 170 + [9.4] + [10.5] * 3 + [9.0] * 2           # buy 9.4, peak +11.7%, falls >10% below top
        rep = bb.simulate({"A": self.cs(prices)}, 0, 10 ** 9)
        self.assertEqual((rep["trades"], rep["trails"], rep["stops"]), (1, 1, 0))
        self.assertLess(rep["pnl"], 0.5)

    def test_stop_loss_35(self):
        prices = [10.0] * 170 + [9.4] + [6.0]
        self.assertEqual(bb.simulate({"A": self.cs(prices)}, 0, 10 ** 9)["stops"], 0)      # no stop loss now
        rep = bb.simulate({"A": self.cs(prices)}, 0, 10 ** 9, sl=0.35)
        self.assertEqual(rep["stops"], 1)
        z = bb.BT_SIZE
        self.assertAlmostEqual(rep["closed_pnl"], z * (1 - 0.003) * 0.65 * (1 - 0.003) - z, places=4)

    def test_never_more_than_max_open_and_cash_not_negative(self):
        data = {s: self.cs([10.0] * 170 + [9.0] * 5) for s in "ABCDEFGH"}
        rep = bb.simulate(data, 0, 10 ** 9)
        self.assertEqual(rep["open"], 6)                         # owner (Nov 18): 6 at a time
        self.assertGreaterEqual(rep["final"], 0)

    def test_backtest_downloads_binance_pages(self):
        now = 1_700_000_000
        def get(url):
            start = int(url.split("startTime=")[1]) // 1000
            n = min(1000, (now - start) // 3600)
            return [[(start + i * 3600) * 1000, "1", "1.1", "0.9", "1.0"] for i in range(n)]
        cs = bb.binance_hours("JUPUSDT", 60, now=now, get=get)
        self.assertGreater(len(cs), 60 * 24 - 5)
        self.assertEqual(len({c["t"] for c in cs}), len(cs))
        rep = bb.backtest(days=30, now=now, get=get)
        self.assertIn("BACKTEST", bb.backtest_text(rep))


class BacktestDeliveryTest(unittest.TestCase):
    def test_result_is_never_held_back(self):
        import telegram_bot as tb
        text = "🧪 BACKTEST — ROBÔ MOEDAS GRANDES (29/05/2026 a 18/11/2026)"
        self.assertTrue(tb.should_send(text, now=1000, chat=1))
        self.assertTrue(tb.should_send(text, now=1001, chat=1))      # second run: sent again


class DemoTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        self.p = [mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_demo_buys_and_sells_on_trailing_without_stop_loss(self):
        bb.demo_reset(1, now=1000)
        d = bb.demo(1)
        d["on"] = True
        bb.set_demo(1, d)
        sigs = [("JUP", "MJ", 0.90, 1.0, True), ("RAY", "MR", 1.80, 2.0, True), ("PYTH", "MP", 0.5, 0.6, True)]
        msgs = []
        send = lambda c, t, *a: msgs.append(t)
        self.assertEqual(bb.demo_run(1, sigs, send, now=2000), ["JUP", "RAY", "PYTH"])   # up to 6, US$ 33.33 each
        self.assertAlmostEqual(bb.demo(1)["cash"], 200 - 3 * 200 / 6)
        bb.demo_exits(1, {"MJ": 0.40, "MR": 1.80}, send, now=3000)                         # -55%: no stop loss, keeps it
        self.assertIn("JUP", bb.demo(1)["pos"])
        bb.demo_exits(1, {"MJ": 1.17, "MR": 1.80}, send, now=4000)                         # +30%: trailing armed
        bb.demo_exits(1, {"MJ": 1.05, "MR": 1.80}, send, now=5000)                         # >10% below the top: sells, +16%
        d = bb.demo(1)
        self.assertNotIn("JUP", d["pos"])
        self.assertEqual(len(d["hist"]), 1)
        self.assertTrue(any("DEMO VENDEU JUP" in m and "dexscreener.com/solana/MJ" in m for m in msgs))
        self.assertTrue(any("DEMO COMPROU JUP" in m and "Moeda: JUP" in m for m in msgs))
        self.assertEqual(bb.DEMO_START, 200)
        text = bb.demo_text(1, {"MR": 1.80}, now=1000 + 31 * 86400)
        self.assertIn("teste de 1 mês completo", text)
        self.assertIn("1 no lucro", text)

    def test_home_is_the_robot_with_demo_and_real(self):
        with mock.patch.object(tp, "positions", return_value=[]), mock.patch.object(tp, "HOME", "robot"):
            data = {b.get("callback_data") for row in tp.home_keyboard(1) for b in row}
            self.assertTrue({"trd_on", "trb_on", "trb_bt", "wallet_withdraw"} <= data)
            self.assertIn("CONTA DEMO", tp.home_text(1))
            self.assertIn("CONTA REAL", tp.home_text(1))


class PositionsTextTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        self.p = [mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save"),
                  mock.patch.object(tp, "positions", return_value=[])]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_lists_demo_positions_with_live_price(self):
        bb.demo_reset(1, now=1000)
        d = bb.demo(1)
        d["on"] = True
        bb.set_demo(1, d)
        bb.demo_run(1, [("JUP", "MJ", 0.90, 1.0, True)], None, now=2000)
        get = lambda url: [{"baseToken": {"address": "MJ"}, "priceUsd": "0.99", "liquidity": {"usd": 1e6}, "priceChange": {}}]
        t = bb.positions_text(1, get=get, now=2000 + 3600)
        self.assertIn("JUP: comprou US$ 33.33 a US$ 0.9", t)
        self.assertIn("+10.0%", t)
        self.assertIn("1/6 abertas", t)
        self.assertIn("dexscreener.com/solana/MJ", t)
        self.assertIn("REAL: 0/6", t)
