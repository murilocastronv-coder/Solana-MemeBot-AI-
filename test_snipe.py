import pathlib
import tempfile
import time
import unittest
from unittest import mock

import snipe_backtest as sbt
import snipe_bot as sb

NOW = 1_800_000_000
MINT = "Coin111pump"


def pair(age_min=10, mcap=30000, vol5=6000, buys=60, sells=20, up5=12, mint=MINT):
    return {"baseToken": {"address": mint, "symbol": "C"}, "dexId": "pumpfun", "marketCap": mcap,
            "volume": {"m5": vol5}, "txns": {"m5": {"buys": buys, "sells": sells}}, "priceChange": {"m5": up5},
            "pairCreatedAt": (NOW - age_min * 60) * 1000, "priceUsd": "0.00003"}


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import live_trade_state
        self.p = mock.patch.object(live_trade_state, "_path", return_value=pathlib.Path(self.tmp.name) / "s.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()


class RuleTest(Tmp):
    def test_entry_rule(self):
        self.assertEqual(sb.entry_reason(pair(), NOW), "")
        self.assertIn("idade", sb.entry_reason(pair(age_min=120), NOW))
        self.assertIn("vendedores", sb.entry_reason(pair(buys=10, sells=30), NOW))
        self.assertIn("movimento", sb.entry_reason(pair(up5=90), NOW))

    def test_exit_rule(self):
        self.assertEqual(sb.exit_signal(0.79, 1.0, 60), ("sell", "stop_loss"))
        self.assertEqual(sb.exit_signal(1.40, 1.60, 60), ("sell", "take_profit"))      # 12% below a +60% top
        self.assertEqual(sb.exit_signal(1.05, 1.10, 31 * 60), ("sell", "time_stop"))
        self.assertEqual(sb.exit_signal(1.10, 1.20, 60)[0], "hold")

    def test_cycle_buys_once_per_coin(self):
        bought = []
        buy = lambda p, m, s, send, chat: bought.append(m) or True
        with mock.patch.object(sb, "_open", return_value=[]), mock.patch.object(sb, "day_pnl", return_value=0):
            self.assertEqual(sb.cycle(pairs=[pair()], now=NOW, buy=buy, safety=lambda m: ""), MINT)
            self.assertIsNone(sb.cycle(pairs=[pair()], now=NOW + 30, buy=buy, safety=lambda m: ""))
        self.assertEqual(bought, [MINT])

    def test_daily_loss_and_max_open_block(self):
        buy = lambda *a: True
        sb.set_paper(False)                                   # the real-money limits
        with mock.patch.object(sb, "_open", return_value=["a", "b", "c"]):
            self.assertIsNone(sb.cycle(pairs=[pair()], now=NOW, buy=buy, safety=lambda m: ""))
        with mock.patch.object(sb, "_open", return_value=[]), mock.patch.object(sb, "day_pnl", return_value=-25):
            self.assertIsNone(sb.cycle(pairs=[pair()], now=NOW, buy=buy, safety=lambda m: ""))


def swap_tx(t, mint, tokens, lamports, payer="P"):
    pre = [{"owner": payer, "mint": mint, "uiTokenAmount": {"amount": "0", "decimals": 6}}]
    post = [{"owner": payer, "mint": mint, "uiTokenAmount": {"amount": str(int(tokens * 1e6)), "decimals": 6}}]
    if tokens < 0:
        pre, post = ([{"owner": payer, "mint": mint, "uiTokenAmount": {"amount": str(int(-tokens * 1e6)), "decimals": 6}}],
                     [{"owner": payer, "mint": mint, "uiTokenAmount": {"amount": "0", "decimals": 6}}])
    return {"blockTime": t, "transaction": {"message": {"accountKeys": [payer]}},
            "meta": {"fee": 5000, "preBalances": [10_000_000_000], "postBalances": [10_000_000_000 + lamports - 5000],
                     "preTokenBalances": pre, "postTokenBalances": post, "logMessages": []}}


class BacktestTest(Tmp):
    def test_trade_parsing_and_create(self):
        tr = sbt.trade_of(swap_tx(100, MINT, 1_000_000, -1_000_000_000), MINT, 150)
        self.assertEqual((tr[0], tr[3]), (100, "buy"))
        self.assertAlmostEqual(tr[1], 150 / 1_000_000)
        create = swap_tx(5, MINT, 100, -10_000_000)
        create["meta"]["logMessages"] = ["Program log: Instruction: Create"]
        self.assertEqual(sbt.created_mint(create), MINT)

    def test_download_and_report_with_fake_helius(self):
        created = NOW - 30 * 86400

        def rpc(method, params):
            addr, opt = params
            gte = opt["filters"]["blockTime"].get("gte", 0)
            if addr in (sbt.PUMP, sb.PUMP_MINT_AUTHORITY):
                tx = swap_tx(gte + 1, MINT, 100, -10_000_000)
                tx["meta"]["logMessages"] = ["Instruction: Create"]
                return {"data": [tx]}
            # coin: steady buying pressure at decision time, then the price doubles after the entry
            if opt["sortOrder"] == "desc":
                t_end = opt["filters"]["blockTime"]["lte"]
                return {"data": [swap_tx(t_end - 200 + i * 10, MINT, 1000, -int((0.05 + i * 0.001) * 1e9 * 1000 / 1000))
                                 for i in range(20)]}
            return {"data": [swap_tx(gte + i * 30, MINT, 1000, -int((0.06 + i * 0.01) * 1e9)) for i in range(40)]}

        with mock.patch.object(sbt, "SAMPLE_POINTS", 24), mock.patch.object(sbt, "DAYS", 1):
            c = sbt.download(rpc, 150, NOW, created - 10, created + 86400)
            self.assertTrue(c["coins"])
            rep = sbt.report(c, created - 10, created + 86400)
            txt = sbt.text(rep)
        self.assertIn("BACKTEST SNIPER", txt)
        self.assertIn("Regra que está no robô real", txt)


if __name__ == "__main__":
    unittest.main()


class LaunchFeedTest(Tmp):
    def test_scan_launches_and_watched_pairs(self):
        def rpc(method, params):
            if method == "getSignaturesForAddress":
                return [{"signature": "s1", "blockTime": NOW - 600}, {"signature": "s2", "blockTime": NOW - 60}]
            return {"meta": {"postTokenBalances": [{"mint": "New" + params[0] + "pump"}]}}
        self.assertEqual(sb.scan_launches(rpc, NOW), 2)
        self.assertEqual(sb._load()["auth_last"], "s1")
        rows = sb.watched_pairs(NOW, fetch=lambda url: [pair(age_min=10, mint="News1pump")])
        self.assertEqual([p["baseToken"]["address"] for p in rows], ["News1pump"])   # s2 is too young (1 min)


class LiveCreatorsTest(Tmp):
    def test_launch_creators_go_to_dev_queue(self):
        import dev_bot as db
        with mock.patch.object(db, "_path", return_value=pathlib.Path(self.tmp.name) / "d.json"):
            def rpc(method, params):
                if method == "getSignaturesForAddress":
                    return [{"signature": "s1", "blockTime": NOW - 600}]
                return {"transaction": {"message": {"accountKeys": ["CREATORZ"]}},
                        "meta": {"postTokenBalances": [{"mint": "Live1pump"}]}}
            sb.scan_launches(rpc, NOW)
            v = db._load()["devs"]["CREATORZ"]
            self.assertEqual((v["tier"], v["found_by"]), ("NA FILA", "live"))
            d = db._load()
            d["devs"]["CREATORZ"].update(tier="MONITORADO", why="carteira com pouco histórico (3 transações)", rated=NOW)
            db._save(d)
            db.enqueue_creators([], NOW + db.PRUNE_S + 10)
            self.assertNotIn("CREATORZ", db._load()["devs"])       # dismissed fresh wallets are forgotten


class SniperWinnerTest(Tmp):
    def test_apply_only_an_out_of_sample_winner(self):
        sbt._put("snipe_bt_winner", {"ok": False})
        self.assertIsNone(sb.apply_winner())
        self.assertFalse(sb.live_ok())
        sbt._put("snipe_bt_winner", {"ok": True, "entry": [5000, 2.0, 5, 30], "exit": [0.15, 0.2, 0.1, 20]})
        names = ("MIN_VOL_5M", "MIN_BUY_RATIO", "MIN_UP_5M", "MAX_UP_5M", "STOP", "ARM", "DROP", "MAX_HOLD_MIN")
        old = {n: getattr(sb, n) for n in names}
        try:
            self.assertTrue(sb.apply_winner())
            self.assertTrue(sb.live_ok())
            self.assertEqual((sb.MIN_VOL_5M, sb.STOP), (5000, 0.15))
            sb.stop_live()
            self.assertFalse(sb.live_ok())
        finally:
            for n, v in old.items():
                setattr(sb, n, v)

    def test_verdict_needs_30_test_trades_profit_and_pf(self):
        self.assertFalse(sbt.verdict_ok({"best": {"test": {"trades": 12, "pnl": 50, "pf": 3}}}))
        self.assertTrue(sbt.verdict_ok({"best": {"test": {"trades": 40, "pnl": 50, "pf": 1.6}}}))
        self.assertFalse(sbt.verdict_ok({"best": {"test": {"trades": 40, "pnl": -5, "pf": 0.9}}}))


class DeadCoinTest(Tmp):
    def test_dead_coin_costs_one_read(self):
        calls = []

        def rpc(method, params):
            calls.append(params[1])
            return {"data": [swap_tx(NOW + 10, MINT, 1000, -int(0.01 * 1e9))]}     # one tiny trade: dead
        out = sbt.download_coin(rpc, MINT, NOW, 150)
        self.assertTrue(out.get("dead"))
        self.assertEqual(len(calls), 1)


class RefillTest(Tmp):
    def test_missing_path_is_no_result_and_refill_fetches_it(self):
        snap = {"price": 0.00001, "mcap": 10000, "vol5": 3000, "buys": 10, "sells": 10, "up5": 3}   # passes only a loose entry
        c = {"points": {}, "coins": {MINT: {"created": NOW, "snaps": {"5": snap}, "after": {}}}}
        s, trades, _ = sbt.simulate(c, NOW - 10, NOW + 10, (2000, 1.0, 0, 100), (0.2, 0.3, 0.12, 30))
        self.assertEqual(trades, [])                                   # not a fake total loss
        calls = []
        rpc = lambda m, p: calls.append(1) or {"data": [swap_tx(NOW + 330, MINT, 1000, -int(0.1 * 1e9))]}
        self.assertEqual(sbt.refill(rpc, c, 150, time.time(), 60), 1)
        self.assertIn("5", c["coins"][MINT]["after"])


class OldCoinsTest(Tmp):
    def test_old_ages_snapshots_and_report(self):
        created = NOW - 30 * 86400
        c = {"points": {}, "coins": {MINT: {"created": created, "snaps": {}, "after": {}},
                                     "DeadCoinpump": {"created": created, "snaps": {}, "after": {}, "dead": True}}}

        def rpc(method, params):
            addr, opt = params
            f = opt["filters"]["blockTime"]
            if opt["sortOrder"] == "desc":                 # busy 5-min window, more buyers
                return {"data": [swap_tx(f["lte"] - 250 + i * 10, addr, 1000, -int(0.3 * 1e9)) for i in range(20)]}
            return {"data": [swap_tx(f["gte"] + i * 60, addr, 1000, -int((0.3 + 0.05 * i) * 1e9)) for i in range(30)]}
        n = sbt.refill_old(rpc, c, 150, time.time(), 60, NOW)
        self.assertEqual(n, 1)                                          # the dead coin is not read
        self.assertTrue(c["coins"][MINT]["old_done"])
        self.assertIn("90", c["coins"][MINT]["snaps"])
        rep = sbt.report(c, created - 10, created + 10, sbt.OLD_AGES)
        self.assertIn("1h30+", sbt.text(rep))


class GradTest(Tmp):
    def test_complete_flag_trade_and_report(self):
        import base64
        import grad_backtest as gb
        raw = bytearray(60)
        raw[48] = 1
        done = base64.b64encode(bytes(raw)).decode()
        rpc = lambda m, p: {"value": [{"data": [done, "base64"]}, {"data": [base64.b64encode(bytes(60)).decode(), "base64"]}]}
        mints = ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]
        self.assertEqual(gb.completed(rpc, mints), {mints[0]: True, mints[1]: False})
        e = {"grad": NOW, "path": [[NOW + 40, 1.0], [NOW + 100, 1.3], [NOW + 200, 1.6]]}
        r = gb.trade(e, 30, ("sl20 tp50", 0.20, 0.50, 0, 0, 240))
        self.assertEqual(r[2], "alvo")
        g = {"points": {"1": 2}, "launch": {"A": NOW, "B": NOW}, "complete": {"A": True, "B": False},
             "coins": {"A": e}}
        txt, ok = gb.report(g, NOW, 1)
        self.assertIn("GRADUOU", txt)
        self.assertFalse(ok)


class GradBotTest(Tmp):
    def test_detect_then_buy_after_delay(self):
        import grad_bot as gbot
        gbot._MEM.clear()
        gbot._load()["enabled"] = True
        gbot.add_launches({"MintA": NOW - 600, "MintB": NOW - 600})
        flags = lambda rpc, ms: {m: m == "MintA" for m in ms}
        bought = []
        buy = lambda p, m, s, send, chat: bought.append(m) or True
        fetch = lambda url: [{"dexId": "pumpswap", "baseToken": {"symbol": "X", "address": "Other"}, "liquidity": {"usd": 9e9}},
                             {"dexId": "pumpswap", "baseToken": {"symbol": "A", "address": "MintA"}, "liquidity": {"usd": 50000}, "marketCap": 90000}]
        self.assertIsNone(gbot.cycle(rpc=object(), now=NOW, buy=buy, fetch=fetch, completed=flags))
        self.assertEqual(gbot.cycle(rpc=object(), now=NOW + 35, buy=buy, fetch=fetch, completed=flags), "MintA")
        self.assertEqual(gbot.cycle(rpc=object(), now=NOW + 50, buy=buy, fetch=fetch, completed=flags), None)
        self.assertEqual(bought, ["MintA"])
        gbot.add_launches({"MintA": NOW})                     # a coin already graduated is not watched again
        self.assertNotIn("MintA", gbot._load()["watch"])
        self.assertEqual(gbot.exit_signal(0.74, 1.0, 10)[1], "stop_loss")
        self.assertEqual(gbot.exit_signal(1.2, 1.6, 10)[1], "take_profit")
        self.assertEqual(gbot.exit_signal(1.1, 1.2, 10)[0], "hold")
        self.assertEqual(gbot.exit_signal(1.1, 1.2, 241 * 60)[1], "time_stop")
        gbot._MEM.clear()


class GradLiquidityTest(Tmp):
    def test_drained_pool_is_sold_and_thin_pool_skipped(self):
        import grad_bot as gbot
        gbot._MEM.clear()
        d = gbot._load()
        d["entry_liq"] = {"M1": 30000, "M2": 30000}
        rows = [{"dexId": "pumpswap", "baseToken": {"address": "M1"}, "liquidity": {"usd": 9000}},   # lost 70%
                {"dexId": "pumpswap", "baseToken": {"address": "M2"}, "liquidity": {"usd": 25000}},
                {"dexId": "pumpswap", "baseToken": {"address": "M3"}, "liquidity": {"usd": 3000}}]   # under 5k
        marked = []
        out = gbot.check_liquidity(NOW, fetch=lambda u: rows, open_mints=["M1", "M2", "M3", "M4"],
                                   mark=lambda m, why: marked.append(m))
        self.assertEqual(sorted(out), ["M1", "M3"])
        gbot._MEM.clear()
        gbot._load()["enabled"] = True
        gbot.add_launches({"Thin": NOW - 600})
        bought = []
        thin = lambda u: [{"dexId": "pumpswap", "baseToken": {"address": "Thin"}, "liquidity": {"usd": 2000}}]
        gbot.cycle(rpc=object(), now=NOW, buy=lambda *a: bought.append(1), fetch=thin, completed=lambda r, ms: {m: True for m in ms})
        gbot.cycle(rpc=object(), now=NOW + 35, buy=lambda *a: bought.append(1), fetch=thin, completed=lambda r, ms: {})
        self.assertEqual(bought, [])
        self.assertEqual(gbot.MAX_OPEN, 4)
        gbot._MEM.clear()


class Sniper1060Test(Tmp):
    def test_score_costs_and_tp(self):
        hi = sb.score(30_000, 25_000, 150, 50, 10)
        lo = sb.score(6_000, 2_100, 5, 30, -10)
        self.assertGreater(hi, 80)
        self.assertLess(lo, 20)
        self.assertLess(sb.score(30_000, 25_000, 150, 50, 150), hi)      # extreme spike scores lower
        self.assertEqual(sb.exit_signal(1.21, 1.21, 60, 0.08, 9, 0, 30, 0.20), ("sell", "take_profit"))
        self.assertEqual(sb.exit_signal(0.91, 1.0, 60, 0.08, 9, 0, 30, 0.20), ("sell", "stop_loss"))
        self.assertEqual(sb.exit_signal(1.05, 1.1, 31 * 60, 0.08, 9, 0, 30, 0.20), ("sell", "time_stop"))
        self.assertTrue(sb.costs_ok(3, 0.20))                              # 6% + 1.3% < 10%
        self.assertFalse(sb.costs_ok(0.5, 0.20))                           # US$ 0.50: fees eat the target

    def test_paper_buy_and_exit(self):
        sb.start_sim()
        self.assertTrue(sb.running())
        msgs = []
        send = lambda chat, text: msgs.append(text)
        p = pair()
        p["priceUsd"] = "0.001"
        self.assertTrue(sb._buy_paper(p, MINT, "TOK", send, 1, now=NOW))
        rows = [{"baseToken": {"address": MINT}, "priceUsd": "0.00125"}]
        self.assertEqual(sb.paper_exits(send, 1, now=NOW + 60, fetch=lambda url: rows), 1)
        h = sb._load()["paper_hist"][-1]
        self.assertEqual(h["exit"], "take_profit")
        self.assertAlmostEqual(h["pnl_usd"], 3 * (1.25 * 0.97 ** 2 - 1) - 0.04, places=4)
        self.assertIn("SIMULAÇÃO", msgs[-1])
        sb.stop_all()
        self.assertFalse(sb.running())


class WalkForwardTest(Tmp):
    def test_groups_report(self):
        c = {"points": {}, "coins": {}}
        start = NOW - 100 * 86400
        for i in range(400):
            created = start + i * 20000
            snap = {"price": 1e-5, "mcap": 20_000, "vol5": 9_000, "buys": 80, "sells": 30, "up5": 8}
            c["coins"]["m" + str(i)] = {"created": created, "snaps": {"10": snap, "15": snap, "20": snap, "30": snap, "45": snap, "55": snap},
                                        "after": {a: [[created + int(a) * 60 + 30, 1e-5], [created + int(a) * 60 + 90, 1.3e-5 if i % 3 else 0.8e-5]]
                                                  for a in ("10", "15", "20", "30", "45", "55")}}
        rep = sbt.report_groups(c, start, start + 400 * 20000)
        self.assertEqual(set(rep["groups"]), {"A", "B", "C"})
        g = rep["groups"]["A"]
        self.assertGreater(g["oos"]["trades"], 30)
        self.assertGreater(g["oos"]["pnl"], 0)                 # 2 of 3 coins +30%: wins after costs
        self.assertIsNotNone(rep["winner"])
        txt = sbt.text_groups(rep)
        self.assertIn("Grupo A (10–20 min)", txt)
        self.assertIn("fora da amostra", txt)
