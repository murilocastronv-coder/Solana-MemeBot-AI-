import os, unittest
from unittest.mock import patch
import live_exit_monitor as m

POS = {"mode": "copy", "token_amount_raw": 1, "cost_usd": 10}


class CopyTrailingStopTest(unittest.TestCase):
    def test_holds_below_arm_without_fixed_stop(self):
        self.assertEqual(m.position_signal(dict(POS), 10, 5)["action"], "hold")      # -50%: only the wallet exits
        self.assertEqual(m.position_signal(dict(POS), 10, 12)["action"], "hold")     # +20%: not armed yet

    def test_arms_and_sells_below_top(self):
        s = m.position_signal(dict(POS), 10, 13)
        self.assertEqual(s["reason"], "trailing")
        self.assertEqual(m.position_signal(dict(POS, peak_ratio=1.6), 10, 15)["action"], "hold")  # 6% off the top
        s = m.position_signal(dict(POS, peak_ratio=1.6), 10, 14)                                  # 12.5% off the top
        self.assertEqual((s["action"], s["reason"]), ("sell", "take_profit"))

    def test_can_be_disabled(self):
        with patch.dict(os.environ, {"COPY_TRAIL_ENABLED": "false"}):
            self.assertEqual(m.position_signal(dict(POS, peak_ratio=1.6), 10, 14)["reason"], "follow_wallet")


if __name__ == "__main__":
    unittest.main()
