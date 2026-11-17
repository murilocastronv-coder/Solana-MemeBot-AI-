import unittest
from unittest import mock

import baixa_bot as bb

H = 3600


def candles(prices):
    return [{"t": i * H, "h": p, "l": p, "c": p} for i, p in enumerate(prices)]


class BaixaTest(unittest.TestCase):
    def setUp(self):
        bb._MEM.clear()
        bb._TOP.clear()

    def test_signal_and_exit(self):
        ok, px, top = bb.signal(candles([10] * 168 + [9.4]))
        self.assertTrue(ok)                                       # -6% from the 7-day top
        self.assertFalse(bb.signal(candles([10] * 168 + [9.6]))[0])
        self.assertFalse(bb.signal(candles([10] * 50 + [5]))[0])  # not enough history: no buy
        self.assertEqual(bb.exit_signal(1.26, 1.26), ("hold", "trailing"))          # armed, still at the top
        self.assertEqual(bb.exit_signal(1.12, 1.26), ("sell", "trailing_stop"))     # 10% below the top
        self.assertEqual(bb.exit_signal(0.70, 1.05), ("hold", "baixa_hold"))        # -30%: above the -35% stop
        self.assertEqual(bb.exit_signal(0.64, 1.05), ("sell", "stop_loss"))         # owner: stop -35%
        self.assertEqual(bb.exit_signal(1.05)[0], "hold")
        with mock.patch.object(bb, "SL", 0.20):
            self.assertEqual(bb.exit_signal(0.80, 1.0), ("sell", "stop_loss"))   # BAIXA_SL=0.20 puts the stop back

    def test_cycle_buys_only_when_on_and_with_sol(self):
        bb._load()["tokens"] = {"BP": {"mint": "MintBP", "pool": "P1", "liq": 900000}}
        bb._load()["resolved_at"] = 1e12
        bb._load()["resolve_v"] = bb.RESOLVE_V
        bought = []
        buy = lambda pair, mint, sym, send, chat: bought.append(sym) or True
        with mock.patch("solid_dip_bt.gecko_hours", return_value=candles([10] * 168 + [9.0])), \
                mock.patch.object(bb, "_open", return_value=set()), mock.patch.object(bb, "day_pnl", return_value=0), \
                mock.patch.object(bb, "dex_prices", side_effect=lambda mints, get=None: {m: 9.0 for m in mints}):
            self.assertEqual(bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150, safety=lambda m: ""), [])   # off
            bb.set_enabled(True)
            self.assertEqual(bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: False, sol_usd=150, safety=lambda m: ""), [])  # no SOL
            self.assertEqual(bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150, safety=lambda m: ""), ["BP"])
        with mock.patch("solid_dip_bt.gecko_hours", return_value=candles([10] * 168 + [9.0])), \
                mock.patch.object(bb, "_open", return_value={"MintBP"}), mock.patch.object(bb, "day_pnl", return_value=0), \
                mock.patch.object(bb, "dex_prices", side_effect=lambda mints, get=None: {m: 9.0 for m in mints}):
            self.assertEqual(bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150, safety=lambda m: ""), [])   # one per coin
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
        got = bb.resolve_majors(get=lambda url: rows if "q=BP" in url else {"pairs": []})
        self.assertEqual(list(got), ["BP"])
        self.assertEqual(got["BP"]["mint"], "Real")

    def test_anti_scam_blocks_the_buy(self):
        bb._load()["tokens"] = {"BP": {"mint": "MintBP", "pool": "P1", "liq": 300000}}
        bb._load()["resolved_at"] = 1e12
        bb._load()["resolve_v"] = bb.RESOLVE_V
        bb.set_enabled(True)
        bought = []
        buy = lambda pair, mint, sym, send, chat: bought.append(sym) or True
        with mock.patch("solid_dip_bt.gecko_hours", return_value=candles([10] * 168 + [9.0])), \
                mock.patch.object(bb, "_open", return_value=set()), mock.patch.object(bb, "day_pnl", return_value=0), \
                mock.patch.object(bb, "dex_prices", side_effect=lambda mints, get=None: {m: 9.0 for m in mints}):
            got = bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150, safety=lambda m: "criador pode congelar carteiras")
            self.assertEqual((got, bought), ([], []))
            again = bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150, safety=lambda m: "")
            self.assertEqual(again, [])                                   # blocked for 24 h
        self.assertIn("anti-golpe", bb._load()["log"][-1])
        self.assertIn("Anti-golpe", bb.status_text())
        self.assertIn("BP (liquidez US$ 300 mil", bb.status_text())
        bb._load()["resolve_v"] = 3                                       # an older robot's list: never shown
        txt = bb.status_text()
        self.assertIn("procurando as moedas da regra nova", txt)
        self.assertNotIn("BP (", txt)
        self.assertNotIn("anti-golpe: criador", txt)

    def test_majors_only_by_official_mint(self):
        jup = bb.MAJORS["JUP"]
        pairs = [{"chainId": "solana", "baseToken": {"symbol": "JUP", "address": jup}, "quoteToken": {"symbol": "SOL"}, "pairAddress": "PJ",
                  "liquidity": {"usd": 5e6}, "volume": {"h24": 2e6}, "marketCap": 1e9}]
        fake = {"pairs": [{"chainId": "solana", "baseToken": {"symbol": "JUP", "address": "FakeJup"}, "quoteToken": {"symbol": "SOL"},
                           "pairAddress": "PX", "liquidity": {"usd": 9e6}, "volume": {"h24": 9e6}, "marketCap": 1e9}]}
        got = bb.resolve_majors(get=lambda url: pairs if "/tokens/v1/" in url else fake)
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
                mock.patch.object(bb, "_open", return_value={"MZEC"}), mock.patch.object(bb, "day_pnl", return_value=0), \
                mock.patch.object(bb, "dex_prices", side_effect=lambda mints, get=None: {m: 9.0 for m in mints}):
            got = bb.cycle(now=2e9, buy=buy, sol_ok=lambda x: True, sol_usd=150, safety=lambda m: "")
        self.assertEqual(len(got), 1)                              # 1 already open + 1 new = 2

    def test_exit_monitor_uses_the_rule(self):
        import live_exit_monitor as lem
        pos = {"mode": "baixa", "cost_usd": 10, "peak_ratio": 1.0}
        self.assertEqual(lem.position_signal(dict(pos, peak_ratio=1.3), 10, 11.5)["reason"], "trailing_stop")
        self.assertEqual(lem.position_signal(pos, 10, 6.4)["reason"], "stop_loss")     # -36%: stop -35%
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
        txt = bb.backtest_text(rep)
        self.assertIn("stop móvel +10%", txt)
        self.assertIn("stop −35%", txt)


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


class SolCheckTest(unittest.TestCase):
    def test_sol_check_uses_live_balance(self):
        with mock.patch("live_engine.get_address", return_value="W"), \
                mock.patch("live_order_bridge.get_sol_balance", return_value=int(0.70555e9)):
            self.assertTrue(bb._sol_ok(72.3))          # 0.7 SOL ~ US$ 51: enough for US$ 23 + US$ 3
        with mock.patch("live_engine.get_address", return_value="W"), \
                mock.patch("live_order_bridge.get_sol_balance", return_value=int(0.1e9)):
            self.assertFalse(bb._sol_ok(72.3))


class WalletTextTest(unittest.TestCase):
    def test_wallet_text(self):
        with mock.patch("live_engine.get_address", return_value="BotWallet111"), \
                mock.patch("live_order_bridge.get_sol_balance", return_value=int(0.70555e9)), \
                mock.patch("wallet_holdings.coins_held", return_value={"MintAAAA": 5.0}):
            txt = bb.wallet_text(sol_usd=72.3)
        self.assertIn("BotWallet111", txt)
        self.assertIn("0.7056 SOL", txt)
        self.assertIn("2 compra(s)", txt)


class TrailNoticeTest(unittest.TestCase):
    def test_trailing_armed_message_shows_where(self):
        import live_engine
        import live_exit_monitor as lem
        sent = []
        lem.ARMED.append(("MintBLOB", {"mode": "baixa", "symbol": "BLOB", "cost_usd": 23.0},
                          {"change_pct": 12.0, "peak_ratio": 1.12, "reason": "trailing", "action": "hold"}))
        with mock.patch.object(live_engine, "sell_signals", return_value=[]), mock.patch.object(live_engine, "_log_positions"), \
                mock.patch.object(live_engine.bridge, "preflight", return_value=True), \
                mock.patch.object(live_engine, "get_address", return_value="W"), \
                mock.patch.object(live_engine, "_safe_sender", side_effect=lambda f: f):
            try:
                live_engine.exit_cycle(lambda chat, text, *a, **k: sent.append(text), 1)
            except Exception:
                pass
        msg = "\n".join(sent)
        self.assertIn("STOP MÓVEL ATIVADO — BLOB", msg)
        self.assertIn("Ativou em: +12.0%", msg)
        self.assertIn("vende se cair para +0.8%", msg)            # 1.12 x 0.90 = 1.008
        self.assertIn("10% abaixo do topo", msg)


class RepairTest(unittest.TestCase):
    def setUp(self):
        bb._MEM.clear()

    def test_copy_trade_never_takes_baixa_and_blob_is_repaired(self):
        import copy_trader as ct
        import live_trade_state as st
        positions = {"8s11PkgQxyz": {"mode": "copy", "copy_orphan": True, "copy_recovered": True, "peak_ratio": 1.116, "symbol": "BLOB"},
                     "OtherMint": {"mode": "copy", "symbol": "X"}}
        fields = {}
        with mock.patch.object(st, "_load", return_value={"positions": positions}), \
                mock.patch.object(st, "set_position_fields", side_effect=lambda m, **f: fields.setdefault(m, f)):
            self.assertEqual(bb.repair_positions(), ["BLOB"])
        self.assertEqual(fields["8s11PkgQxyz"]["mode"], "baixa")
        self.assertEqual(fields["8s11PkgQxyz"]["peak_ratio"], 1.0)          # no instant sale at a loss
        self.assertNotIn("OtherMint", fields)
        src = open(ct.__file__, encoding="utf-8").read()
        self.assertIn('"rocket", "baixa")', src)                          # recover_real_orphans skips the robot's positions


class LiveSignalTest(unittest.TestCase):
    def setUp(self):
        bb._TOP.clear()

    def test_top_cached_and_price_from_dexscreener(self):
        tk = {"mint": "M1", "pool": "P1", "liq": 1e5}
        calls = []
        with mock.patch("solid_dip_bt.gecko_hours", side_effect=lambda *a, **k: calls.append(1) or candles([10] * 170)):
            self.assertEqual(bb.live_signal("X", tk, 1000, None, {"M1": 9.4}), (True, 9.4, 10))
            self.assertEqual(bb.live_signal("X", tk, 2000, None, {"M1": 9.8})[0], False)
        self.assertEqual(len(calls), 1)                                   # one GeckoTerminal read per hour
        with mock.patch("solid_dip_bt.gecko_hours", return_value=[]):     # GeckoTerminal refuses: last top kept
            self.assertEqual(bb.live_signal("X", tk, 1000 + 7200, None, {"M1": 9.0})[2], 10)
        self.assertEqual(bb.live_signal("X", tk, 1000, None, {}), (False, 0.0, 0.0))   # no price: no buy
        rows = [{"baseToken": {"address": "M1"}, "priceUsd": "1.5", "liquidity": {"usd": 100}},
                {"baseToken": {"address": "M1"}, "priceUsd": "1.6", "liquidity": {"usd": 900}}]
        self.assertEqual(bb.dex_prices(["M1"], get=lambda url: rows), {"M1": 1.6})
