import os
import tempfile
import time
import unittest
from unittest.mock import patch

import trading_sim as t

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def pair(mint=MINT, **over):
    p = {"chainId": "solana", "baseToken": {"address": mint, "symbol": "GOOD"}, "priceUsd": "0.01",
         "liquidity": {"usd": 400_000}, "fdv": 4_000_000, "volume": {"h1": 300_000},
         "priceChange": {"m5": 2, "h1": 9, "h6": 20},
         "txns": {"m5": {"buys": 150, "sells": 90}, "h1": {"buys": 1500, "sells": 1100}},
         "pairCreatedAt": (time.time() - 2 * 86400) * 1000}
    p.update(over)
    return p


class TradingSimTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start(); self.addCleanup(p.stop)
        t._tried.clear(); t._safety_cache.clear(); t._RECENT.clear()
        self.price = {"tokens": 1000, "sell_sol": 0}
        patches = [patch.object(t, "_sol_usd", return_value=200.0),
                   patch.object(t, "safety_reason", return_value=""),
                   patch.object(t, "_order", side_effect=self.order)]
        for x in patches:
            x.start(); self.addCleanup(x.stop)

    def order(self, inp, out, amount):
        if inp == t.SOL_MINT:
            return {"outAmount": str(self.price["tokens"])}
        return {"outAmount": str(self.price["sell_sol"])}

    def test_off_by_default_and_no_real_orders(self):
        self.assertEqual(t.buy_cycle([pair()]), [])
        self.assertFalse(t.enabled())

    def test_buy_then_take_profit(self):
        t.set_enabled(True)
        opened = t.buy_cycle([pair()])
        self.assertEqual(len(opened), 1)
        self.assertAlmostEqual(t._load()["balance"], 100 - 10 - t.NETWORK_FEE_USD, places=6)
        self.price["sell_sol"] = int(12 / 200 * 1e9)      # worth US$ 12 now (+20%)
        closed = t.exit_cycle()
        self.assertEqual(closed[0]["reason"], "take_profit")
        self.assertGreater(closed[0]["pnl_usd"], 1.9)
        self.assertEqual(t._load()["positions"], {})

    def test_stop_loss_and_hold(self):
        t.set_enabled(True)
        t.buy_cycle([pair()])
        self.price["sell_sol"] = int(9.8 / 200 * 1e9)     # -2%: hold
        self.assertEqual(t.exit_cycle(), [])
        self.price["sell_sol"] = int(9.0 / 200 * 1e9)     # -10%: stop
        self.assertEqual(t.exit_cycle()[0]["reason"], "stop_loss")

    def test_five_positions_max(self):
        t.set_enabled(True)
        from solders.keypair import Keypair
        rows = [pair(str(Keypair().pubkey())) for _ in range(7)]
        t.buy_cycle(rows)
        self.assertEqual(len(t._load()["positions"]), 5)
        self.assertIn("5 posições", t.hour_text())

    def test_unsafe_and_weak_rejected(self):
        t.set_enabled(True)
        with patch.object(t, "safety_reason", return_value="criador pode congelar carteiras"):
            self.assertEqual(t.buy_cycle([pair()]), [])
        self.assertEqual(t.buy_cycle([pair("Weak111111111111111111111111111111111111111", volume={"h1": 1000})]), [])
        text = t.hour_text()
        self.assertIn("congelar", text)
        self.assertIn("pouco volume", text)

    def test_restart_keeps_positions(self):
        t.set_enabled(True)
        t.buy_cycle([pair()])
        t._tried.clear()  # simulates a restart: in-memory state lost
        self.assertEqual(t.buy_cycle([pair()]), [])   # same token not bought twice
        self.assertEqual(len(t._load()["positions"]), 1)

    def test_quote_failure_is_reported(self):
        t.set_enabled(True)
        t.buy_cycle([pair()])
        with patch.object(t, "_order", side_effect=RuntimeError("rpc down")):
            self.assertEqual(t.exit_cycle(), [])
        self.assertIn("cotação de venda falhou", t.status_text())


if __name__ == "__main__":
    unittest.main()
