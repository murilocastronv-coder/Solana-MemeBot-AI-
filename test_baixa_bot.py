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
        bb._load()["resolve_v"] = bb.RESOLVE_V
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

    def test_resolve_picks_real_pool_not_fake(self):
        sol = {"symbol": "SOL"}
        rows = {"pairs": [
            {"chainId": "solana", "baseToken": {"symbol": "BP", "address": "Fake"}, "quoteToken": sol, "pairAddress": "PF",
             "liquidity": {"usd": 1.25e9}, "volume": {"h24": 900}, "marketCap": 1e6},                     # absurd liquidity
            {"chainId": "solana", "baseToken": {"symbol": "BP", "address": "Quiet"}, "quoteToken": sol, "pairAddress": "PQ",
             "liquidity": {"usd": 900000}, "volume": {"h24": 1000}, "marketCap": 5e6},                    # no real volume
            {"chainId": "solana", "baseToken": {"symbol": "BP", "address": "Real"}, "quoteToken": {"symbol": "USDC"}, "pairAddress": "PR",
             "liquidity": {"usd": 400000}, "volume": {"h24": 300000}, "marketCap": 8e6},
            {"chainId": "solana", "baseToken": {"symbol": "BP", "address": "Odd"}, "quoteToken": {"symbol": "XYZ"}, "pairAddress": "PO",
             "liquidity": {"usd": 500000}, "volume": {"h24": 900000}, "marketCap": 8e6}]}
        got = bb.resolve(get=lambda url: rows if "q=BP" in url else {"pairs": []})
        self.assertEqual(list(got), ["BP"])
        self.assertEqual(got["BP"]["mint"], "Real")

    def test_majors_only_by_official_mint(self):
        jup = bb.MAJORS["JUP"]
        pairs = [{"chainId": "solana", "baseToken": {"symbol": "JUP", "address": jup}, "quoteToken": {"symbol": "SOL"}, "pairAddress": "PJ",
                  "liquidity": {"usd": 5e6}, "volume": {"h24": 2e6}, "marketCap": 1e9}]
        fake = {"pairs": [{"chainId": "solana", "baseToken": {"symbol": "JUP", "address": "FakeJup"}, "quoteToken": {"symbol": "SOL"},
                           "pairAddress": "PX", "liquidity": {"usd": 9e6}, "volume": {"h24": 9e6}, "marketCap": 1e9}]}
        got = bb.resolve(get=lambda url: pairs if "/tokens/v1/" in url else fake)
        self.assertEqual(got["JUP"]["mint"], jup)                 # the fake copy found by name is never used
        self.assertNotIn("ZEC", bb.COINS)

    def test_max_two_open_of_23(self):
        self.assertEqual((bb.USD, bb.MAX_OPEN), (23, 2))
        bb._load()["tokens"] = {s: {"mint": "M" + s, "pool": "P" + s, "liq": 1e6} for s in ("BP", "ZEC", "SPCX", "HYPE")}
        bb._load()["resolved_at"] = 1e12
        bb._load()["resolve_v"] = bb.RESOLVE_V
        bb.set_enabled(True)
        bought = []
        buy = lambda pair, mint, sym, send, chat: bought.append(sym) or True
        with mock.patch("solid_dip_bt.gecko_hours", return_value=candles([10] * 168 + [9.0])), \
                mock.patch.object(bb, "_open", return_value={"MZEC"}), mock.patch.object(bb, "day_pnl", return_value=0):
            got = bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150)
        self.assertEqual(len(got), 1)                              # 1 already open + 1 new = 2

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
        bb._load()["resolve_v"] = bb.RESOLVE_V
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


class AllCoinsTest(unittest.TestCase):
    def test_backtest_all_filters_age_liquidity_and_trades(self):
        import random
        import time as _t
        now = 1_800_000_000
        iso = lambda t: _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(t))
        pools = [{"attributes": {"name": "FROG / SOL", "address": "P1", "reserve_in_usd": "300000", "pool_created_at": iso(now - 300 * 86400)}},
                 {"attributes": {"name": "BABY / SOL", "address": "P2", "reserve_in_usd": "500000", "pool_created_at": iso(now - 3 * 86400)}},
                 {"attributes": {"name": "TINY / SOL", "address": "P3", "reserve_in_usd": "50000", "pool_created_at": iso(now - 99 * 86400)}},
                 {"attributes": {"name": "USDC / SOL", "address": "P4", "reserve_in_usd": "9e7", "pool_created_at": iso(now - 900 * 86400)}},
                 {"attributes": {"name": "BIG / SOL", "address": "P5", "reserve_in_usd": "900000", "pool_created_at": iso(now - 99 * 86400)}}]
        rnd = random.Random(3)

        def get(url):
            if "/pools?page=1" in url:
                return {"data": pools}
            if "/pools?page=" in url:
                return {"data": []}
            rows, p = [], 1.0
            for k in range(600):
                p *= rnd.uniform(0.95, 1.06)
                rows.append([now - (k + 1) * 4 * 3600, p, p * 1.03, p * 0.97, p, 1])
            return {"data": {"attributes": {"ohlcv_list": rows}}}
        found = bb.all_coins(now, now + 999, get)
        self.assertIn("FROG", found)
        self.assertFalse({"BABY", "TINY", "USDC", "BIG", "JUP"} & set(found))      # > US$ 400 mil is out too
        rep = bb.backtest_all(now, get)
        self.assertIn("FROG", rep["coins"])
        self.assertGreater(rep["full"]["trades"], 0)
        self.assertIn("TODAS AS MOEDAS", bb.backtest_all_text(rep))
