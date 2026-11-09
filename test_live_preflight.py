import unittest
from unittest.mock import patch
from live_preflight import live_status, require_live_ready, risk_settings

class LivePreflightTests(unittest.TestCase):
    def test_live_is_always_disabled(self):
        with patch.dict("os.environ", {"LIVE_TRADING_ENABLED": "true", "SOLANA_BOT_PUBLIC_ADDRESS": "11111111111111111111111111111111"}):
            self.assertFalse(live_status()["enabled"])
            self.assertFalse(live_status()["ready"])
    def test_signing_is_unavailable(self):
        with self.assertRaises(RuntimeError):
            require_live_ready()
    def test_limits(self):
        self.assertEqual(risk_settings()["order_usd"], 10)
        self.assertEqual(risk_settings()["daily_loss_limit_usd"], 3)
if __name__ == "__main__":
    unittest.main()
