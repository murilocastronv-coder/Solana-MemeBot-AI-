import time
import unittest
from unittest.mock import patch

import live_engine as e
import live_exit_monitor as m


def hot(**over):
    p = {"chainId": "solana", "baseToken": {"address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"},
         "priceUsd": "0.01", "liquidity": {"usd": 400_000}, "fdv": 4_000_000,
         "volume": {"h24": 6_000_000, "h1": 500_000},
         "priceChange": {"m5": 2, "h1": 9, "h6": 20},
         "txns": {"m5": {"buys": 150, "sells": 90}, "h1": {"buys": 1500, "sells": 1100}},
         "pairCreatedAt": (time.time() - 2 * 86400) * 1000}
    p.update(over)
    return p


class HotTests(unittest.TestCase):
    def test_hot_token_passes(self):
        self.assertEqual(e.hot_reasons(hot()), [])

    def test_low_volume_rejected(self):
        self.assertIn("pouco volume na última hora", e.hot_reasons(hot(volume={"h24": 6_000_000, "h1": 20_000})))
        self.assertTrue(any("volume 24h" in r for r in e.hot_reasons(hot(volume={"h24": 50_000, "h1": 500_000}))))

    def test_spike_and_selling_rejected(self):
        self.assertTrue(e.hot_reasons(hot(priceChange={"m5": 15, "h1": 9, "h6": 20})))
        self.assertTrue(e.hot_reasons(hot(txns={"m5": {"buys": 50, "sells": 90}, "h1": {"buys": 1500, "sells": 1100}})))

    def test_hot_exits(self):
        now = time.time()
        pos = {"mode": "hot", "opened_at": now, "peak_ratio": 0}
        self.assertEqual(m.position_signal(pos, 10, 9.3)["reason"], "stop_loss")       # -7%
        self.assertEqual(m.position_signal(pos, 10, 10.2)["action"], "hold")
        armed = dict(pos, peak_ratio=1.10)
        self.assertEqual(m.position_signal(armed, 10, 10.4)["reason"], "take_profit")  # 5%+ below peak
        old = dict(pos, opened_at=now - 50 * 60)
        self.assertEqual(m.position_signal(old, 10, 10.1)["reason"], "time_stop")


if __name__ == "__main__":
    unittest.main()
