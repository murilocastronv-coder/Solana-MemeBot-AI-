import pathlib
import tempfile
import unittest
from unittest import mock

import dev_bot as db

NOW = 1_800_000_000
DEV = "DEVwallet1111111111111111111111111111111111"


def pos(mint, pnl, res, closed, opened=None, cost=10.0, hold=40):
    return {"mint": mint, "pnl_usd": pnl, "result_pct": res, "closed": closed, "opened": opened or closed - 600,
            "cost_closed": cost, "hold_min": hold}


def pair(mint="TOK", price="1.0", liq=50_000, vol5=8_000, up5=5):
    return {"baseToken": {"address": mint, "symbol": mint}, "dexId": "pumpswap", "priceUsd": price, "liquidity": {"usd": liq},
            "volume": {"m5": vol5}, "priceChange": {"m5": up5}, "pairCreatedAt": (NOW - 3600) * 1000}


class DevBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(db, "_path", return_value=pathlib.Path(self.tmp.name) / "d.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_elite_dev_and_rejections(self):
        good = [pos("M" + str(i), 20, 0.5, NOW - i * 20000) for i in range(32)] + \
               [pos("X", -2, -0.1, NOW - 99 * 3600)]
        s = db.rate_dev(good, NOW)
        self.assertEqual(s["tier"], "ELITE")
        one_coin = [pos("ONE", 20, 0.5, NOW - i * 3600) for i in range(30)]
        self.assertIn("tokens", db.rate_dev(one_coin, NOW)["why"])
        rug = good + [pos("R", -9, -0.95, NOW - 5)]
        self.assertIn("rug", db.rate_dev(rug, NOW)["why"])

    def test_dev_move(self):
        tx = {"meta": {"preTokenBalances": [{"owner": DEV, "mint": "T", "uiTokenAmount": {"amount": "1000"}}],
                       "postTokenBalances": [{"owner": DEV, "mint": "T", "uiTokenAmount": {"amount": "500"}}],
                       "preBalances": [10], "postBalances": [20]},
              "transaction": {"message": {"accountKeys": [DEV]}}}
        kind, frac, _ = db.dev_move(tx, "T", DEV)
        self.assertEqual((kind, frac), ("sell", 0.5))

    @mock.patch.object(db, "STOP_LOSS", 0.08)
    @mock.patch.object(db, "TP1", 0.15)
    @mock.patch.object(db, "MIRROR_DEV", False)
    def test_sim_buy_exits_and_metrics(self):
        info = {"tier": "ELITE", "score": 90, "win": 0.9}
        self.assertEqual(db.buy_reason(pair(), info), "")
        self.assertIn("liquidez", db.buy_reason(pair(liq=5000), info))
        self.assertIn("não aprovado", db.buy_reason(pair(), {"tier": "MONITORADO"}))
        self.assertTrue(db.sim_buy(pair(), DEV, "teste", NOW))
        self.assertFalse(db.sim_buy(pair(), DEV, "teste", NOW))            # one entry per token
        db.exits({"TOK": 1.2}, NOW + 60)                                      # TP1: sells 50%
        self.assertTrue(db._load()["positions"]["TOK"]["tp1"])
        db.exits({"TOK": 1.05}, NOW + 120)                                    # trailing 8% under the 1.2 top
        self.assertEqual(db._load()["positions"], {})
        m = db.metrics()
        self.assertEqual(m["trades"], 1)
        db.sim_buy(pair("B"), DEV, "t", NOW)
        db.exits({"B": 0.9}, NOW + 60)                                        # stop -8%
        self.assertEqual(db._load()["history"][-1]["why_out"][:4], "stop")
        db.sim_buy(pair("C"), DEV, "t", NOW)
        db.exits({"C": 1.0}, NOW + 60, dev_sells={"C": 0.8})                  # dev sold 80%: close
        self.assertNotIn("C", db._load()["positions"])

    def test_backtest_uses_only_past_trades(self):
        hist = {DEV: [pos("M" + str(i), 5 if i % 10 else -5, 0.4 if i % 10 else -0.2, NOW + i * 3600, NOW + i * 3600 - 600)
                      for i in range(60)]}
        res = db.backtest(hist)
        self.assertIn("A: todos os DEVs", res)
        self.assertGreater(res["A: todos os DEVs"]["teste"]["trades"], 0)
        self.assertIn("FORA DA AMOSTRA", db.backtest_text(res, 1))


if __name__ == "__main__":
    unittest.main()


class DevCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(db, "_path", return_value=pathlib.Path(self.tmp.name) / "d.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_full_cycle_copies_an_approved_dev_buy(self):
        txs = {"create": {"transaction": {"message": {"accountKeys": [DEV]}}, "meta": {}},
               "buy": {"meta": {"preTokenBalances": [], "postTokenBalances": [
                   {"owner": DEV, "mint": "TOK", "uiTokenAmount": {"amount": "200"}}],
                   "preBalances": [2_000_000_000], "postBalances": [1_000_000_000]}, "transaction": {"message": {"accountKeys": [DEV]}}}}
        calls = {"n": 0}

        supply = {"amount": "10000"}

        def rpc(method, params):
            if method == "getTokenSupply":
                return {"value": supply}
            if params[0] == "TOK":
                return [{"signature": "create"}]
            if params[1].get("limit") == db.QUICK_SIGS:
                return [{"signature": "h" + str(i)} for i in range(db.QUICK_SIGS)]
            calls["n"] += 1
            return [{"signature": "old"}] if calls["n"] == 1 else [{"signature": "buy"}]

        good = [pos("M" + str(i), 20, 0.5, NOW - i * 20000) for i in range(32)] + [pos("X", -2, -0.1, NOW - 99 * 3600)]
        kw = dict(rpc=rpc, pairs=[pair()], get_tx=lambda r, s: txs.get(s, {}), sol_usd=200,
                  safety=lambda m: "", analyze=lambda *a, **k: ({}, good, ""), prices_fn=lambda ms, bm: {"TOK": 1.0})
        db.cycle(now=NOW, **kw)          # finds the creator, rates the dev, remembers its last tx
        self.assertEqual(db._load()["devs"][DEV]["tier"], "ELITE")
        db.cycle(now=NOW + 60, **kw)     # the dev buys its own token (5% of the supply) -> simulated copy
        self.assertIn("TOK", db._load()["positions"])

    def test_dev_holding_too_much_is_not_copied(self):
        self.assertEqual(db.dev_share(lambda m, p: {"value": {"amount": "1000"}}, "T", 500), 0.5)
        self.assertGreater(0.5, db.MAX_DEV_SHARE)


class DevBacktestButtonTest(unittest.TestCase):
    def test_downloads_devs_and_reports_capital_60(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(db, "_path", return_value=pathlib.Path(t) / "d.json"):
            d = db._load()
            d["devs"] = {DEV: {"tokens_seen": []}}
            db._save(d)
            closed = [pos("M" + str(i) + "pump", 5 if i % 10 else -5, 0.4 if i % 10 else -0.2, NOW - i * 20000, NOW - i * 20000 - 600)
                      for i in range(80)] + [pos("RAYcoin", 50, 3.0, NOW - 5000, NOW - 5600)]
            txt = db.run_backtest(rpc=object(), sol_usd=200, analyze=lambda *a, **k: ({}, closed, ""), now=NOW)
            self.assertIn("US$ 60", txt)
            self.assertIn("baixados (meta 10,000)", txt)
            self.assertIn("Histórico disponível", txt)
            self.assertTrue(db._load()["devs"][DEV]["hist"])
            self.assertIn("só moedas da pump.fun", txt)
            self.assertIn("80 operações reais", txt)          # the Raydium trade is left out


class DevBacktestEmptyTest(unittest.TestCase):
    def test_no_devs_yet_does_not_crash(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(db, "_path", return_value=pathlib.Path(t) / "d.json"):
            txt = db.run_backtest(rpc=None, now=NOW)
            self.assertIn("sem histórico", txt)


class DevTransferScamTest(unittest.TestCase):
    def test_hidden_transfer_sells_and_blocks(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(db, "_path", return_value=pathlib.Path(t) / "d.json"):
            d = db._load()
            d["devs"] = {DEV: {"tier": "ELITE", "score": 90, "win": 0.9, "trades": 40, "pf": 3, "rated": NOW, "last_sig": "old"}}
            d["tokens"] = {"TOK": {"dev": DEV, "seen": NOW, "symbol": "TOK", "last_sig": "old"}}
            db._save(d)
            db.sim_buy(pair(), DEV, "t", NOW)
            moved = {"meta": {"preTokenBalances": [{"owner": DEV, "mint": "TOK", "uiTokenAmount": {"amount": "1000"}}],
                              "postTokenBalances": [{"owner": DEV, "mint": "TOK", "uiTokenAmount": {"amount": "100"}}],
                              "preBalances": [100], "postBalances": [99]}, "transaction": {"message": {"accountKeys": [DEV]}}}
            rpc = lambda m, p: [{"signature": "mv"}]
            db.cycle(rpc=rpc, pairs=[pair()], now=NOW + 60, get_tx=lambda r, s: moved, sol_usd=200,
                     prices_fn=lambda ms, bm: {"TOK": 1.0})
            self.assertNotIn("TOK", db._load()["positions"])
            self.assertEqual(db._load()["devs"][DEV]["tier"], "BLOQUEADO")



class PumpOnlyTest(unittest.TestCase):
    def test_non_pump_coin_is_not_copied(self):
        info = {"tier": "ELITE", "score": 90, "win": 0.9}
        self.assertIn("pump.fun", db.buy_reason(dict(pair(), dexId="raydium"), info))
        self.assertEqual(db.buy_reason(pair(), info), "")



class MirrorDevTest(unittest.TestCase):
    def test_only_the_dev_sells_and_the_same_share(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(db, "_path", return_value=pathlib.Path(t) / "d.json"):
            db.sim_buy(pair(), DEV, "t", NOW)
            db.exits({"TOK": 0.5}, NOW + 60)                     # -50%: no stop, keeps holding (like the dev)
            db.exits({"TOK": 3.0}, NOW + 120)                    # +200%: no take profit
            self.assertIn("TOK", db._load()["positions"])
            q = db._load()["positions"]["TOK"]["qty"]
            db.exits({"TOK": 3.0}, NOW + 180, dev_sells={"TOK": 0.3})   # dev sold 30% -> sells 30%
            self.assertAlmostEqual(db._load()["positions"]["TOK"]["qty"], q * 0.7)
            db.exits({"TOK": 3.0}, NOW + 240, dev_sells={"TOK": 1.0})   # dev sold everything -> everything
            self.assertNotIn("TOK", db._load()["positions"])



class QuickDismissTest(unittest.TestCase):
    def test_wallet_with_few_transactions_is_dismissed_cheaply(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(db, "_path", return_value=pathlib.Path(t) / "d.json"):
            d = db._load()
            d["devs"] = {DEV: {"tokens_seen": []}}
            db._save(d)
            analyze = mock.Mock()
            self.assertEqual(db.audit_devs(lambda m, p: [{"signature": "x"}] * 5, 200, NOW, analyze), 1)
            analyze.assert_not_called()
            self.assertIn("pouco histórico", db._load()["devs"][DEV]["why"])


class DevRunupTest(unittest.TestCase):
    def test_price_already_up_since_dev_buy_is_skipped(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(db, "_path", return_value=pathlib.Path(t) / "d.json"):
            info = {"tier": "ELITE", "score": 90, "win": 0.9, "trades": 40, "pf": 3}
            # dev paid 1 SOL (US$ 200) for 400 tokens = US$ 0.50; now US$ 1.00 (+100%)
            mv = {"mint": "TOK", "kind": "buy", "frac": 1.0, "held": 400, "sol": -1_000_000_000, "tokens": 400}
            db.handle_move(mv, DEV, info, {"blockTime": NOW - 5}, {"TOK": pair()}, lambda m, p: {"value": {"amount": "100000"}},
                           200, NOW, safety=lambda m: "")
            self.assertNotIn("TOK", db._load()["positions"])

    def test_curve_coin_needs_no_pool_liquidity(self):
        p = dict(pair(), dexId="pumpfun", liquidity={"usd": 0}, marketCap=30000)
        info = {"tier": "ELITE", "score": 90, "win": 0.9, "trades": 40, "pf": 3}
        self.assertNotIn("liquidez", db.buy_reason(p, info) or "")

    def test_emergency_stop_pauses_and_sells(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(db, "_path", return_value=pathlib.Path(t) / "d.json"):
            db.set_enabled(True)
            db.sim_buy(pair(), DEV, "t", NOW)
            self.assertEqual(db.emergency_stop(now=NOW + 1), 1)
            self.assertFalse(db.enabled())
            self.assertEqual(db._load()["positions"], {})


class DevPeriodsTest(unittest.TestCase):
    def test_period_table_and_daily_limit(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(db, "_path", return_value=pathlib.Path(t) / "d.json"):
            d = db._load()
            d["devs"] = {DEV: {"hist": [{"opened": NOW - i * 3 * 86400, "closed": NOW - i * 3 * 86400 + 600, "result_pct": 0.4,
                                         "pnl_usd": 5, "mint": "M" + str(i) + "pump"} for i in range(60)]}}
            db._save(d)
            txt = db.periods_text(now=NOW)
            for k in ("7 dias", "30 dias", "90 dias", "180 dias", "NÃO CALCULADO", "US$ 500"):
                self.assertIn(k, txt)
            with mock.patch.object(db, "DAILY_LOSS_USD", 10), mock.patch.object(db, "metrics", return_value={"pnl_day": -12}):
                self.assertFalse(db.sim_buy(pair(), DEV, "t", NOW))
