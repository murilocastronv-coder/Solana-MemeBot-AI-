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
            self.assertEqual(done, ["A", "B"])                      # max 2 open
            self.assertEqual(rules.call_count, 2)
            positions.clear()
            self.assertEqual(bb.run_for(1, sigs[:1], lambda *a, **k: None, now=6000, buy=buy), [])   # 24 h cooldown
        self.assertEqual(bought, [("MA", 0.2), ("MB", 0.2)])

    def test_exit_rules_match_the_strategy(self):
        import user_trade as ut
        p = {"sl": bb.SL, "trail": bb.TRAIL}
        self.assertEqual(ut.exit_reason(p, 0.64, 1.0), "sl")
        self.assertIsNone(ut.exit_reason(p, 0.70, 1.0))
        self.assertEqual(ut.exit_reason(p, 1.0, 1.12), "trail")    # armed at +12%, 10% below the top
        self.assertIsNone(ut.exit_reason(p, 1.05, 1.08))           # never reached +10%: no trailing
        with mock.patch.object(tp, "_load", return_value={"tp": 0, "sl": 0, "trail": 0}):
            self.assertEqual(tp.exit_signal({"trade_sl": bb.SL, "trade_trail": bb.TRAIL}, 0.6, 1.0), ("sell", "trade_sl"))
            self.assertEqual(tp.exit_signal({"trade_sl": bb.SL, "trade_trail": bb.TRAIL}, 1.0, 1.12), ("sell", "trade_trail"))


if __name__ == "__main__":
    unittest.main()
