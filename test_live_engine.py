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
             patch("live_trade_state._sol_to_usd", return_value=46.0):
            live_engine.exit_cycle(self.send, "1")
        st = state.status()
        self.assertEqual((st["positions"], st["pending"], st["daily_loss_usd"]), (0, 0, 0.0))
        self.assertTrue(st["enabled"])
        msg = self.sent[-1]
        for part in ("LUCRO", "VENDEU BONK", "US$ 40.00", "US$ 46.00", "+6.00 USD (+15.0%)", "P/L total: +6.00", "1/1"):
            self.assertIn(part, msg)
        self.assertIn("COMPROU BONK", self.sent[1])
        self.assertEqual(state.pnl_summary()["total_pnl_usd"], 6.0)
        self.assertIn("US$ 40.00 de BONK (20% da conta de US$ 200.00)", self.sent[0])
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
        self.assertIn("BLOQUEADO", self.sent[-1])
        self.assertFalse(state.can_open()[0])
        with self.assertRaises(RuntimeError):
            state.start()

if __name__ == "__main__":
    unittest.main()
