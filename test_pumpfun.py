import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, Mock

import pumpfun

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def token(**over):
    t = {"id": MINT, "symbol": "PEPE2", "launchpad": "pump.fun",
         "firstPool": {"createdAt": (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()}}
    t.update(over)
    return t


class DiscoveryTests(unittest.TestCase):
    """pump.fun list used only to widen the MEV scan (no trading)."""

    def test_only_pumpfun_tokens(self):
        self.assertTrue(pumpfun.is_pumpfun({"id": "abcpump", "launchpad": ""}))
        self.assertFalse(pumpfun.is_pumpfun({"id": MINT, "launchpad": "bonk.fun"}))

    def test_feed_error_is_reported(self):
        pumpfun._cache[:] = [0.0, []]
        with patch("pumpfun.jupiter_rate_limiter.get", side_effect=RuntimeError("401 Unauthorized")):
            self.assertEqual(pumpfun.fetch(), [])
        self.assertIn("401", pumpfun.last_scan["error"])

    def test_feed_is_parsed(self):
        pumpfun._cache[:] = [0.0, []]
        resp = Mock(); resp.json.return_value = [token(), {"id": "other", "launchpad": "raydium"}]
        resp.raise_for_status.return_value = None
        with patch("pumpfun.jupiter_rate_limiter.get", return_value=resp):
            rows = pumpfun.fetch()
        self.assertEqual([t["id"] for t in rows], [MINT])


if __name__ == "__main__":
    unittest.main()
