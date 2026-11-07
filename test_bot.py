import unittest
from unittest.mock import patch
import bot

class RiskTests(unittest.TestCase):
    def pair(self, **changes):
        p = {"chainId":"solana", "baseToken":{"address":"token","symbol":"T"},
             "pairCreatedAt": 1000000000, "liquidity":{"usd":100000},
             "volume":{"h24":50000}, "fdv":1000000}
        p.update(changes)
        return p

    def test_healthy_market_still_watch_only(self):
        r = bot.assess_pair(self.pair(), now_ms=1000000000 + 48*3600000)
        self.assertEqual(r["status"], "WATCH_ONLY")
        self.assertIn("onchain_security_not_verified", r["reasons"])
        self.assertEqual(r["category"], "new")

    def test_low_liquidity_rejected(self):
        r = bot.assess_pair(self.pair(liquidity={"usd":100}), now_ms=1000000000 + 48*3600000)
        self.assertEqual(r["status"], "REJECT")

    def test_unknown_age_rejected(self):
        r = bot.assess_pair(self.pair(pairCreatedAt=None))
        self.assertIn("unknown_pair_age", r["reasons"])

    def test_established(self):
        r = bot.assess_pair(self.pair(), now_ms=1000000000 + 240*3600000)
        self.assertEqual(r["category"], "established")

    def test_trading_hard_disabled(self):
        self.assertFalse(bot.TRADING_ENABLED)

if __name__ == "__main__":
    unittest.main()
