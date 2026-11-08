import os, tempfile, time, unittest
from unittest.mock import patch
import live_engine, live_trade_state as state, sim_engine, trade_report
from test_live_engine import LiveEngineTests, pair, new_pair, MINT, SAFE_MINT


class RoundtripTests(unittest.TestCase):
    def setUp(self):
        live_engine._roundtrip_cache.clear()

    def run_rt(self, back_ratio):
        def quote(inp, out, amount, taker=None):
            if out == MINT:
                return {"outAmount": "1000000"}
            return {"outAmount": str(int(50_000_000 * back_ratio))}
        with patch("live_engine.bridge.sol_usd_price", return_value=200), \
             patch("live_engine.bridge.order", side_effect=quote):
            return live_engine.roundtrip_ok(MINT, usd=10.0)

    def test_honest_token_passes(self):
        self.assertTrue(self.run_rt(0.97)[0])

    def test_taxed_or_honeypot_token_fails(self):
        ok, why = self.run_rt(0.70)
        self.assertFalse(ok)
        self.assertIn("70%", why)


class HistoryTests(LiveEngineTests):
    def test_real_trade_goes_to_history(self):
        live_engine.start(); self.side = "buy"
        live_engine.buy_cycle([pair()], self.send, "1")
        self.side = "sell"
        position = state._load()["positions"][MINT]
        sig = {"action": "sell", "reason": "stop_loss", "change_pct": -9.0}
        with patch("live_engine.sell_signals", return_value=[(MINT, position, sig)]), \
             patch("live_trade_state._sol_to_usd", return_value=18.0):
            live_engine.exit_cycle(self.send, "1")
        h = state.history()
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["symbol"], h[0]["mode"], h[0]["reason"], h[0]["pnl_usd"]), ("BONK", "hot", "stop_loss", -2.0))
        text = trade_report.history_text(h, sim_engine.summary())
        self.assertIn("Volume: 1 operações | acerto 0% | P/L -2.00", text)
        self.assertIn("🔻 BONK [V] -2.00 USD (-10%) · stop", text)


class SimTests(LiveEngineTests):
    def setUp(self):
        super().setUp()
        self.value = 10.0
        for p in (patch("sim_engine.order", return_value={"outAmount": "1000"}),
                  patch("sim_engine.quote_value_usd", side_effect=lambda m, a: self.value),
                  patch("sim_engine._jupiter_healthy", return_value=True),
                  patch("live_engine.sniper_fetch", return_value=[new_pair()]),
                  patch("live_engine.holders_ok", return_value=(True, ""))):
            p.start(); self.addCleanup(p.stop)

    def test_off_by_default_and_never_touches_real_ledger(self):
        self.assertEqual(sim_engine.buy_cycle([pair()], self.send, "1"), 0)
        sim_engine.set_enabled(True)
        self.assertEqual(sim_engine.buy_cycle([pair()], self.send, "1"), 1)
        self.assertEqual(state.status()["positions"], 0)   # real ledger untouched
        self.assertIn("🧪 SIM COMPROU BONK — US$ 10.00", self.sent[-1])

    def test_full_simulated_cycle_with_trailing_exit(self):
        sim_engine.set_enabled(True)
        sim_engine.buy_cycle([pair()], self.send, "1")   # 10% of US$ 100
        self.value = 10.0 * 0.99 * 1.5                     # +50% after slippage
        sim_engine.exit_cycle(self.send, "1")
        self.assertEqual(sim_engine.summary()["open"], 1)  # trailing: lets it run
        self.value = 10.0 * 0.99 * 1.3                     # dropped > 10% from peak
        sim_engine.exit_cycle(self.send, "1")
        s = sim_engine.summary()
        self.assertEqual(s["open"], 0)
        self.assertEqual(s["history"][-1]["reason"], "take_profit")
        self.assertAlmostEqual(s["equity"], 100 + (10.0 * 1.3 * 0.99 * 0.99 - 10.0), places=2)
        self.assertIn("SIM 💰 VENDEU BONK", self.sent[-1])

    def test_simulated_sniper(self):
        sim_engine.set_enabled(True)
        self.assertEqual(sim_engine.sniper_cycle(self.send, "1"), 1)
        pos = list(sim_engine._load()["positions"].values())[0]
        self.assertEqual((pos["mode"], pos["cost_usd"]), ("sniper", 15.0))


if __name__ == "__main__":
    unittest.main()
