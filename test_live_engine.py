import json, os, tempfile, time, unittest
from unittest.mock import patch
import live_engine, live_trade_state as state, live_order_bridge as bridge

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WALLET = "11111111111111111111111111111112"

def pair():
    return {"baseToken": {"address": MINT, "symbol": "BONK"}, "priceUsd": "0.00002",
            "liquidity": {"usd": 900000}, "volume": {"h24": 500000}, "fdv": 1500000,
            "pairCreatedAt": (time.time() - 30 * 86400) * 1000,
            "priceChange": {"m5": 1, "h1": 4, "h6": 9, "h24": 5}, "txns": {"h1": {"buys": 120, "sells": 80}}}

SAFE_MINT = {"value": {"data": {"parsed": {"type": "mint", "info": {"mintAuthority": None, "freezeAuthority": None}}}}}

class LiveEngineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(self.dir.name, "w.json"),
                                           "LIVE_TRADING_ENABLED": "true", "BOT_WALLET_ENCRYPTION_PASSPHRASE": "x" * 24})
        self.env.start()
        live_engine._tried.clear()
        live_engine._safety.clear()
        live_engine._sniper_seen.clear()
        live_engine._fetch_cache[:] = [0.0, []]
        live_engine._roundtrip_cache.clear()
        live_engine._last_notice.clear()
        self.mint_info = SAFE_MINT
        self.sent = []
        self.chain = {"token": 0, "sol": 1_000_000_000}
        patches = [
            patch("live_engine.get_address", return_value=WALLET),
            patch("live_engine.bridge.preflight", return_value=True),
            patch("live_order_bridge.preflight", return_value=True),
            patch("bot_wallet_service.get_address", return_value=WALLET),
            patch("live_order_bridge.sol_for_usd", return_value=50_000_000),
            patch("live_order_bridge.sol_usd_price", return_value=200),
            patch("live_engine.rpc", side_effect=lambda m, p: self.mint_info),
            patch("live_engine.roundtrip_ok", return_value=(True, "")),
            patch("live_order_bridge.order", side_effect=lambda *a, **k: {"requestId": "r", "outAmount": "1", "transaction": "x"}),
            patch("live_order_bridge.sign_order", return_value="signed"),
            patch("live_order_bridge.execute_signed", side_effect=self.execute),
            patch("live_order_bridge.wait_for_finalized", return_value={"finalized": True}),
            patch("live_order_bridge.get_token_balance", side_effect=lambda o, m: self.chain["token"]),
            patch("live_order_bridge.get_sol_balance", side_effect=lambda o: self.chain["sol"]),
            patch("solana_settlement.get_token_balance", side_effect=lambda o, m: self.chain["token"]),
            patch("solana_settlement.get_sol_balance", side_effect=lambda o: self.chain["sol"]),
            patch("live_swap_executor.rpc", return_value={"transaction": {"message": {"accountKeys": [{"pubkey": WALLET, "signer": True}]}}, "meta": {"err": None}}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def execute(self, signed, request_id):
        if self.side == "buy":
            self.chain["token"] += 5000; self.chain["sol"] -= 50_000_000
        else:
            self.chain["token"] = 0; self.chain["sol"] += 60_000_000
        return {"signature": "S" * 88}

    def send(self, chat, text, *a):
        self.sent.append(text)

    def test_off_until_started(self):
        self.assertIsNone(live_engine.buy_cycle([pair()], self.send, "1"))
        self.assertEqual(self.sent, [])

    def test_buy_then_take_profit_sell(self):
        live_engine.start()
        self.side = "buy"
        result = live_engine.buy_cycle([pair()], self.send, "1")
        self.assertEqual(result["status"], "settled_buy")
        self.assertEqual(state.status()["positions"], 1)
        self.side = "sell"
        signal = {"action": "sell", "reason": "take_profit", "change_pct": 20.0}
        position = state._load()["positions"][MINT]
        with patch("live_engine.sell_signals", return_value=[(MINT, position, signal)]), \
             patch("live_trade_state._sol_to_usd", return_value=23.0):
            live_engine.exit_cycle(self.send, "1")
        st = state.status()
        self.assertEqual((st["positions"], st["pending"], st["daily_loss_usd"]), (0, 0, 0.0))
        self.assertTrue(st["enabled"])
        msg = self.sent[-1]
        for part in ("LUCRO", "VENDEU BONK", "US$ 20.00", "US$ 23.00", "+3.00 USD (+15.0%)", "P/L total: +3.00", "1/1"):
            self.assertIn(part, msg)
        self.assertIn("COMPROU BONK", self.sent[1])
        self.assertEqual(state.pnl_summary()["total_pnl_usd"], 3.0)
        self.assertIn("US$ 20.00 de BONK (10% da conta de US$ 200.00)", self.sent[0])
        self.assertEqual(state.daily_loss_limit(), 30.0)

    def test_reports_why_nothing_was_bought(self):
        live_engine.start()
        live_engine._last_notice.clear()
        young = pair(); young["pairCreatedAt"] = time.time() * 1000
        self.assertIsNone(live_engine.buy_cycle([young], self.send, "1"))
        self.assertIn("0 passaram nos filtros", self.sent[-1])
        self.assertIn("1 com idade < 24h", self.sent[-1])
        live_engine.buy_cycle([young], self.send, "1")
        self.assertEqual(len(self.sent), 1)  # throttled to once per hour

    def test_two_buys_per_scan_max_three_open(self):
        live_engine.start(); self.side = "buy"
        mints = ["DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
                 "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"]
        rows = []
        for m in mints:
            p = pair(); p["baseToken"] = {"address": m, "symbol": m[:4]}; rows.append(p)
        live_engine.buy_cycle(rows, self.send, "1")
        self.assertEqual(state.status()["positions"], 2)   # 2 per scan
        live_engine.buy_cycle(rows, self.send, "1")
        self.assertEqual(state.status()["positions"], 3)   # capped at 3 open
        self.assertEqual(sum("COMPROU" in m for m in self.sent), 3)

    def test_skips_token_without_uptrend(self):
        live_engine.start()
        falling = pair(); falling["priceChange"]["h1"] = -3
        self.assertIsNone(live_engine.buy_cycle([falling], self.send, "1"))
        self.assertIn("sem tendência de alta", self.sent[-1])

    def test_anti_rug_blocks_mint_authority(self):
        live_engine.start()
        self.mint_info = {"value": {"data": {"parsed": {"type": "mint", "info": {"mintAuthority": "X", "freezeAuthority": None}}}}}
        self.assertIsNone(live_engine.buy_cycle([pair()], self.send, "1"))
        self.assertEqual(state.status()["pending"], 0)
        self.assertIn("criar mais moedas", self.sent[-1])

    def test_uncertain_buy_blocks_robot(self):
        live_engine.start()
        self.side = "buy"
        with patch("live_order_bridge.execute_signed", side_effect=RuntimeError("timeout")):
            live_engine.buy_cycle([pair()], self.send, "1")
        self.assertEqual(state.status()["pending"], 1)
        self.assertIn("Vou conferir sozinho", self.sent[-1])
        self.assertFalse(state.can_open()[0])
        with self.assertRaises(RuntimeError):
            state.start()

def age_pending(seconds=600):
    from datetime import datetime, timezone, timedelta
    data = state._load()
    for p in data["pending"].values():
        p["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    state._save(data)


class RecoveryTests(LiveEngineTests):
    def test_buy_never_executed_is_released(self):
        live_engine.start(); self.side = "buy"
        with patch("live_order_bridge.execute_signed", side_effect=RuntimeError("timeout")):
            live_engine.buy_cycle([pair()], self.send, "1")
        self.assertEqual(live_engine.recover(self.send, "1")[0]["status"], "waiting")  # tx could still land
        self.assertEqual(state.status()["pending"], 1)
        age_pending()
        result = live_engine.recover(self.send, "1")
        self.assertEqual(result[0]["status"], "discarded")
        self.assertEqual(state.status()["pending"], 0)
        self.assertTrue(state.can_open()[0])
        self.assertIn("não foi executada", self.sent[-1])

    def test_unstick_button_resolves_and_reports(self):
        live_engine.start(); self.side = "buy"
        with patch("live_order_bridge.execute_signed", side_effect=RuntimeError("timeout")):
            live_engine.buy_cycle([pair()], self.send, "1")
        self.assertIn("ordem pendente", live_engine.why_stopped())
        age_pending(90)
        live_engine.unstick(self.send, "1")
        self.assertIn("ORDENS PENDENTES", self.sent[-3])
        self.assertIn("não executada", self.sent[-2])
        self.assertEqual(self.sent[-1], "🟢 Robô ligado e operando.")

    def test_buy_landed_but_unconfirmed_becomes_position(self):
        live_engine.start(); self.side = "buy"
        with patch("live_swap_executor.rpc", return_value=None), patch("live_engine.time.sleep"):
            live_engine.buy_cycle([pair()], self.send, "1")
        self.assertEqual(state.status()["pending"], 1)
        age_pending()
        with patch("live_swap_executor.rpc", return_value=None):
            result = live_engine.recover(self.send, "1")
        self.assertEqual(result[0]["status"], "settled_buy")
        st = state.status()
        self.assertEqual((st["pending"], st["positions"]), (0, 1))
        self.assertEqual(state._load()["positions"][MINT]["cost_usd"], 20.0)

    def test_sell_landed_but_unconfirmed_settles_pnl(self):
        live_engine.start(); self.side = "buy"
        live_engine.buy_cycle([pair()], self.send, "1")
        self.side = "sell"
        position = state._load()["positions"][MINT]
        signal = {"action": "sell", "reason": "stop_loss", "change_pct": -9.0}
        with patch("live_engine.sell_signals", return_value=[(MINT, position, signal)]), \
             patch("live_swap_executor.rpc", return_value=None), patch("live_engine.time.sleep"):
            live_engine.exit_cycle(self.send, "1")
        age_pending()
        with patch("live_swap_executor.rpc", return_value=None), \
             patch("live_trade_state._sol_to_usd", return_value=18.0):
            result = live_engine.recover(self.send, "1")
        self.assertEqual(result[0]["status"], "settled_sell")
        st = state.status()
        self.assertEqual((st["pending"], st["positions"]), (0, 0))
        self.assertAlmostEqual(state.pnl_summary()["total_pnl_usd"], -2.0)
        self.assertIn("-2.00 USD", self.sent[-1])


class SellFailureTests(LiveEngineTests):
    def open_position(self):
        live_engine.start(); self.side = "buy"
        live_engine.buy_cycle([pair()], self.send, "1")
        live_engine._sell_failed.clear()
        live_engine._sell_fail_count.clear()
        self.side = "sell"
        return state._load()["positions"][MINT]

    def sl(self, position):
        return patch("live_engine.sell_signals", return_value=[(MINT, position, {"action": "sell", "reason": "stop_loss", "change_pct": -9.0})])

    def test_rejected_sell_releases_immediately_with_reason(self):
        import jupiter_v2_executor
        position = self.open_position()
        with self.sl(position), patch("live_order_bridge.execute_signed",
                                      side_effect=jupiter_v2_executor.OrderError("Slippage exceeded")):
            live_engine.exit_cycle(self.send, "1")
        self.assertEqual(state.status()["pending"], 0)
        self.assertTrue(state.can_open()[0])
        self.assertIn("Slippage exceeded", self.sent[-1])
        with self.sl(position):
            live_engine.exit_cycle(self.send, "1")      # retry is delayed 60 s
        self.assertEqual(state.status()["positions"], 1)

    def test_quote_failure_releases_immediately(self):
        position = self.open_position()
        with self.sl(position), patch("live_order_bridge.order", side_effect=RuntimeError("no route")):
            live_engine.exit_cycle(self.send, "1")
        self.assertEqual(state.status()["pending"], 0)
        self.assertIn("no route", self.sent[-1])

    def test_sells_what_wallet_really_holds(self):
        position = self.open_position()
        self.chain["token"] = 4900  # 2% transfer fee
        seen = {}
        real = live_engine.bridge.submit_swap
        def spy(mint, side, amount, wallet):
            seen["amount"] = amount
            return real(mint, side, amount, wallet)
        with self.sl(position), patch("live_engine.bridge.submit_swap", side_effect=spy), \
             patch("live_trade_state._sol_to_usd", return_value=18.0):
            live_engine.exit_cycle(self.send, "1")
        self.assertEqual(seen["amount"], 4900)
        self.assertEqual(state.status()["positions"], 0)

    def test_token_gone_from_wallet_is_removed(self):
        position = self.open_position()
        self.chain["token"] = 0
        with self.sl(position):
            live_engine.exit_cycle(self.send, "1")
        self.assertEqual(state.status()["positions"], 0)
        self.assertIn("não tem mais", self.sent[-1])


class DeadTokenTests(SellFailureTests):
    def test_dead_token_written_off_after_10_min(self):
        import live_exit_monitor as m
        self.open_position()
        m._quote_fail_since.clear()
        def quote(inp, out, amount, taker=None):
            if inp == MINT:
                raise RuntimeError("no route")
            return {"outAmount": "20000000", "requestId": "r"}
        with patch("live_exit_monitor.order", side_effect=quote):
            live_engine.exit_cycle(self.send, "1")
            self.assertEqual(state.status()["positions"], 1)          # not yet: could be temporary
            m._quote_fail_since[MINT] -= 601
            live_engine.exit_cycle(self.send, "1")
        st = state.status()
        self.assertEqual(st["positions"], 0)
        self.assertTrue(st["enabled"])                                 # keeps running
        self.assertIn("MOEDA ZERADA", self.sent[-1])
        self.assertAlmostEqual(state.pnl_summary()["total_pnl_usd"], -20.0)

    def test_jupiter_outage_never_writes_off(self):
        import live_exit_monitor as m
        self.open_position()
        m._quote_fail_since.clear()
        with patch("live_exit_monitor.order", side_effect=RuntimeError("Jupiter down")):
            m._quote_fail_since[MINT] = -10_000
            live_engine.exit_cycle(self.send, "1")
        self.assertEqual(state.status()["positions"], 1)

    def test_unsellable_token_written_off_after_5_failures(self):
        import jupiter_v2_executor
        position = self.open_position()
        with self.sl(position), patch("live_order_bridge.execute_signed",
                                      side_effect=jupiter_v2_executor.OrderError("honeypot")):
            for _ in range(6):
                live_engine._sell_failed.clear()
                live_engine.exit_cycle(self.send, "1")
        self.assertEqual(state.status()["positions"], 0)
        self.assertIn("5 tentativas", self.sent[-1])


def new_pair(minutes=20):
    return {"baseToken": {"address": MINT, "symbol": "NEW"}, "priceUsd": "0.0001",
            "liquidity": {"usd": 25000}, "fdv": 300000, "pairCreatedAt": (time.time() - minutes * 60) * 1000,
            "priceChange": {"m5": 12, "h1": 60}, "volume": {"m5": 15000},
            "txns": {"m5": {"buys": 90, "sells": 40}, "h1": {"buys": 400, "sells": 250}}}


class SniperTests(LiveEngineTests):
    def rpc(self, method, params):
        if method == "getAccountInfo":
            return self.mint_info
        if method == "getTokenSupply":
            return {"value": {"amount": "1000"}}
        if method == "getTokenLargestAccounts":
            return {"value": [{"amount": "700"}] + [{"amount": str(self.holder)}] * 10}

    def setUp(self):
        super().setUp()
        self.holder = 1
        p = patch("live_engine.rpc", side_effect=self.rpc)
        p.start(); self.addCleanup(p.stop)

    def test_candidate_rules(self):
        self.assertTrue(live_engine.sniper_candidate(new_pair()))
        self.assertFalse(live_engine.sniper_candidate(new_pair(minutes=90)))
        weak = new_pair(); weak["txns"]["m5"] = {"buys": 30, "sells": 40}
        self.assertFalse(live_engine.sniper_candidate(weak))

    def test_sniper_off_by_default(self):
        live_engine.start()
        with patch("live_engine.sniper_fetch", return_value=[new_pair()]):
            self.assertIsNone(live_engine.sniper_cycle(self.send, "1"))

    def test_sniper_buys_5pct_and_marks_position(self):
        live_engine.start(); state.set_sniper(True); self.side = "buy"
        with patch("live_engine.sniper_fetch", return_value=[new_pair()]):
            result = live_engine.sniper_cycle(self.send, "1")
        self.assertEqual(result["status"], "settled_buy")
        pos = state._load()["positions"][MINT]
        self.assertEqual((pos["mode"], pos["cost_usd"]), ("sniper", 10.0))  # 5% of US$ 200
        self.assertIn("SNIPER COMPROU NEW", self.sent[-1])

    def test_sniper_skips_concentrated_holders(self):
        live_engine.start(); state.set_sniper(True); self.holder = 40  # 10 x 4% = 40%
        with patch("live_engine.sniper_fetch", return_value=[new_pair()]):
            self.assertIsNone(live_engine.sniper_cycle(self.send, "1"))
        self.assertEqual(state.status()["positions"], 0)

    def test_time_stop_and_wide_stop(self):
        import live_exit_monitor as m
        live_engine.start(); state.set_sniper(True); self.side = "buy"
        with patch("live_engine.sniper_fetch", return_value=[new_pair()]):
            live_engine.sniper_cycle(self.send, "1")
        def evaluate(value_usd):
            with patch("live_exit_monitor.order", side_effect=[{"outAmount": "1"}, {"outAmount": str(int(value_usd * 1e6))}]):
                return [s for _, _, s in m._evaluate()][0]
        self.assertEqual(evaluate(9.0)["action"], "hold")          # -10%: inside sniper -15% stop
        self.assertEqual(evaluate(8.4)["reason"], "stop_loss")     # -16%
        data = state._load(); data["positions"][MINT]["opened_at"] -= 3600; state._save(data)
        self.assertEqual(evaluate(10.5)["reason"], "time_stop")    # 1h, never took off


if __name__ == "__main__":
    unittest.main()
