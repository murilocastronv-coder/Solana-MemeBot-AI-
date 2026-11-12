import os
import tempfile
import time
import unittest
from unittest.mock import patch

import dip_bot
import signals as sg

NOW = time.time()
MINT = "DIPcoin1111111111111111111111111111111pump"


def pair(**kw):
    p = {"chainId": "solana", "dexId": "pumpswap", "pairAddress": "POOL", "baseToken": {"address": MINT, "symbol": "DIP"},
         "quoteToken": {"address": sg.SOL_MINT}, "liquidity": {"usd": 60_000}, "pairCreatedAt": (NOW - 3 * 3600) * 1000,
         "txns": {"h1": {"buys": 300, "sells": 200}, "m5": {"buys": 30, "sells": 12}}, "volume": {"h24": 300_000},
         "priceChange": {"m5": 3, "h1": -25, "h6": 20, "h24": 120}, "priceUsd": "0.001"}
    p.update(kw)
    return p


class DipBotTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_strong_coin_in_a_sharp_dip_that_stopped_falling(self):
        self.assertEqual(dip_bot.dip_reason(pair(), NOW), "")

    def test_rejections(self):
        self.assertIn("sem queda", dip_bot.dip_reason(pair(priceChange={"m5": 3, "h1": -5, "h6": 20, "h24": 120}), NOW))
        self.assertIn("rug", dip_bot.dip_reason(pair(priceChange={"m5": 3, "h1": -60, "h6": 20, "h24": 120}), NOW))
        self.assertIn("ainda caindo", dip_bot.dip_reason(pair(priceChange={"m5": -2, "h1": -25, "h6": 20, "h24": 120}), NOW))
        self.assertIn("fraca", dip_bot.dip_reason(pair(priceChange={"m5": 3, "h1": -25, "h6": -10, "h24": 5}), NOW))
        self.assertIn("vendedores", dip_bot.dip_reason(pair(txns={"h1": {"buys": 100, "sells": 300},
                                                                  "m5": {"buys": 30, "sells": 12}}), NOW))
        self.assertIn("pump.fun", dip_bot.dip_reason(pair(dexId="raydium", baseToken={"address": "X", "symbol": "X"}), NOW))
        self.assertIn("liquidez alta", dip_bot.dip_reason(pair(liquidity={"usd": 400_000}), NOW))
        self.assertIn("nova demais", dip_bot.dip_reason(pair(pairCreatedAt=(NOW - 300) * 1000), NOW))

    def test_cycle_buys_once_and_not_again_within_the_cooldown(self):
        bought = []
        buyer = lambda p, send, chat: bought.append(p["baseToken"]["address"]) or ""
        dip_bot.cycle(pairs=[pair()], now=NOW, safety=lambda m, curve=False: "", buyer=buyer)
        dip_bot.cycle(pairs=[pair()], now=NOW + 60, safety=lambda m, curve=False: "", buyer=buyer)
        self.assertEqual(bought, [MINT])

    def test_scam_is_not_bought(self):
        bought = []
        dip_bot.cycle(pairs=[pair()], now=NOW, safety=lambda m, curve=False: "freeze authority ativa",
                      buyer=lambda p, s, c: bought.append(1) or "")
        self.assertEqual(bought, [])

    def test_off_does_nothing(self):
        dip_bot.set_enabled(False)
        self.assertEqual(dip_bot.cycle(pairs=[pair()], now=NOW, safety=lambda m, curve=False: "",
                                       buyer=lambda p, s, c: ""), [])

    def test_exit_rules(self):
        import live_exit_monitor as lem
        pos = {"mode": "dip", "peak_ratio": 0, "opened_at": time.time()}
        self.assertEqual(lem.position_signal(pos, 10, 7.9)["reason"], "stop_loss")          # -21%
        self.assertEqual(lem.position_signal(pos, 10, 11)["action"], "hold")
        self.assertEqual(lem.position_signal(dict(pos, peak_ratio=1.5), 10, 13.5)["action"], "sell")  # 10% under top
        old = dict(pos, opened_at=time.time() - 7 * 3600)
        self.assertEqual(lem.position_signal(old, 10, 10.5)["reason"], "time_stop")

    def test_status_text(self):
        self.assertIn("DIP BUYING BOT", dip_bot.status_text())


if __name__ == "__main__":
    unittest.main()
