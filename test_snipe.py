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
        with mock.patch.object(sb, "_open", return_value=["a", "b"]):
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
            if addr == sbt.PUMP:
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


class LaunchTestTest(Tmp):
    def test_replay_entry_delays_and_mirror_exit(self):
        import dev_bot as db
        import launch_test as lt
        with mock.patch.object(db, "_path", return_value=pathlib.Path(self.tmp.name) / "d.json"):
            d = db._load()
            d["devs"] = {"DEVX": {"tier": "ELITE", "win": 0.9, "trades": 30, "launch_mints": [MINT],
                                  "hist": [{"mint": MINT, "opened": NOW, "closed": NOW + 600, "sells": [[NOW + 600, 1.0]]}]}}
            db._save(d)
            self.assertEqual(len(lt.candidates()), 1)

            def rpc(method, params):
                addr, opt = params
                gte = opt["filters"]["blockTime"]["gte"]
                if gte >= NOW + 600:                       # price after the dev sold: 2x the launch price
                    return {"data": [swap_tx(NOW + 601, MINT, 1000, -int(0.002 * 1e9))]}
                # launch: price 0.001 at +3 s, 0.0015 at +12 s, 0.003 at +40 s (per 1000 tokens, in SOL)
                return {"data": [swap_tx(NOW + 3, MINT, 1000, -int(0.001 * 1e9)),
                                 swap_tx(NOW + 12, MINT, 1000, -int(0.0015 * 1e9)),
                                 swap_tx(NOW + 40, MINT, 1000, -int(0.003 * 1e9))]}
            r = lt.replay(rpc, MINT, NOW, [[NOW + 600, 1.0]], 150)
            self.assertGreater(r[2], 0.5)          # in at 0.001, out at 0.002: about +100% minus costs
            self.assertLess(r[30], 0)              # in at 0.003, out at 0.002: loss
            txt = lt.text([("DEVX", MINT, r)], 1)
            self.assertIn("Entrando 2 s depois", txt)


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
