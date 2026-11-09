import unittest
from live_exit_rules import trailing_exit_signal as sig

class TrailingTests(unittest.TestCase):
    def test_stop_loss(self):
        self.assertEqual(sig(10, 9.1)["reason"], "stop_loss")
    def test_holds_below_arm(self):
        self.assertEqual(sig(10, 11.4)["action"], "hold")
    def test_lets_profit_run(self):
        r = sig(10, 15, peak_ratio=1.5)
        self.assertEqual((r["action"], r["reason"]), ("hold", "trailing"))
    def test_sells_10pct_below_peak(self):
        r = sig(10, 13.4, peak_ratio=1.5)
        self.assertEqual((r["action"], r["reason"]), ("sell", "take_profit"))
        self.assertAlmostEqual(r["peak_pct"], 50.0)
    def test_new_peak_is_tracked(self):
        self.assertAlmostEqual(sig(10, 20, peak_ratio=1.5)["peak_ratio"], 2.0)

if __name__ == "__main__":
    unittest.main()
