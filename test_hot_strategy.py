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


class SniperFilterTests(unittest.TestCase):
    def test_no_socials_rejected(self):
        from test_live_engine import new_pair
        import pumpfun
        from test_pumpfun import token
        p = new_pair(); self.assertTrue(e.sniper_candidate(p))
        p["info"] = {}; self.assertFalse(e.sniper_candidate(p))
        self.assertTrue(pumpfun.candidate(token()))
        self.assertFalse(pumpfun.candidate(token(twitter=None)))
        self.assertFalse(pumpfun.candidate(token(isSus=True)))
        self.assertFalse(pumpfun.candidate(token(organicScoreLabel="low")))

    def test_sniper_reasons_match_candidate(self):
        from test_live_engine import new_pair
        self.assertEqual(e.sniper_reasons(new_pair()), [])
        self.assertIn("fora de 5–60 min de vida", e.sniper_reasons(new_pair(minutes=90)))
        weak = new_pair(); weak["txns"]["m5"] = {"buys": 5, "sells": 40}
        self.assertIn("compra fraca", e.sniper_reasons(weak))


class DailyLimitTests(unittest.TestCase):
    def test_fixed_30_usd(self):
        import live_trade_state as st
        self.assertEqual(st._loss_limit({"max_daily_loss_usd": 11.46}), 30.0)
        self.assertEqual(st.set_daily_loss_limit(500), 30.0)


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
