import unittest
from wallet_trading import can_open_trade, paper_exit_signal, live_trade

class TradingTests(unittest.TestCase):
    def test_live_always_blocked(self):
        allowed, reasons = can_open_trade(equity_usd=50, daily_realized_pnl_usd=0, open_positions=0, liquidity_usd=100000)
        self.assertFalse(allowed)
        self.assertIn("live_execution_not_implemented", reasons)

    def test_daily_loss_limit(self):
        _, reasons = can_open_trade(equity_usd=50, daily_realized_pnl_usd=-3, open_positions=0, liquidity_usd=100000)
        self.assertIn("daily_loss_limit", reasons)

    def test_exit_signals(self):
        self.assertEqual(paper_exit_signal(100, 91), "PAPER_STOP_LOSS")
        self.assertEqual(paper_exit_signal(100, 116), "PAPER_TAKE_PROFIT")

    def test_no_live_signing(self):
        with self.assertRaises(RuntimeError):
            live_trade()

if __name__ == "__main__":
    unittest.main()
