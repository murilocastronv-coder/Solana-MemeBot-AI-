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


class HotExitTests(unittest.TestCase):
    def test_hot_exits(self):
        now = time.time()
        pos = {"mode": "hot", "opened_at": now, "peak_ratio": 0}
        self.assertEqual(m.position_signal(pos, 10, 9.3)["reason"], "stop_loss")       # -7%
        self.assertEqual(m.position_signal(pos, 10, 10.2)["action"], "hold")
        armed = dict(pos, peak_ratio=1.10)
        self.assertEqual(m.position_signal(armed, 10, 10.4)["reason"], "take_profit")  # 5%+ below peak
        old = dict(pos, opened_at=now - 50 * 60)
        self.assertEqual(m.position_signal(old, 10, 10.1)["reason"], "time_stop")


class DailyLimitTests(unittest.TestCase):
    def test_no_daily_limit_by_default(self):
        import live_trade_state as st
        self.assertFalse(st.DAILY_LOSS_LIMIT_ENABLED)
        self.assertEqual(st._loss_limit({"max_daily_loss_usd": 11.46}), float("inf"))
        self.assertEqual(st.set_daily_loss_limit(500), float("inf"))


class SyncTests(unittest.TestCase):
    def test_drops_positions_sold_by_hand(self):
        positions = {"A": {"symbol": "AAA", "token_amount_raw": "1000"},
                     "B": {"symbol": "BBB", "token_amount_raw": "1000"},
                     "C": {"symbol": "CCC", "token_amount_raw": "1000"}}
        held = {"A": 0, "B": 5, "C": 990}
        dropped, sent = [], []
        with patch.object(e, "get_address", return_value="W"), \
             patch.object(e.state, "pending_orders", return_value=[]), \
             patch.object(e.state, "_load", return_value={"positions": positions}), \
             patch.object(e.state, "drop_position", side_effect=dropped.append), \
             patch.object(e.bridge, "get_token_balance", side_effect=lambda w, m: held[m]):
            e.sync_positions(lambda c, t: sent.append(t), "1")
        self.assertEqual(dropped, ["A", "B"])
        self.assertEqual(len(sent), 2)

    def test_rpc_error_never_drops(self):
        dropped = []
        with patch.object(e, "get_address", return_value="W"), \
             patch.object(e.state, "pending_orders", return_value=[]), \
             patch.object(e.state, "_load", return_value={"positions": {"A": {"token_amount_raw": "1"}}}), \
             patch.object(e.state, "drop_position", side_effect=dropped.append), \
             patch.object(e.bridge, "get_token_balance", side_effect=RuntimeError("rpc")):
            e.sync_positions(lambda c, t: None, "1")
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()
