import unittest
from unittest.mock import patch

from wallet_copy import empty_state, process_confirmed_block, best_wallets
import copy_trader


def tx(owner, mint, amount, sol_delta, timestamp=1000):
    pre = 10_000_000_000
    before = [] if amount >= 0 else [{"owner": owner, "mint": mint,
        "uiTokenAmount": {"amount": str(-amount), "decimals": 0}}]
    after = [] if amount <= 0 else [{"owner": owner, "mint": mint,
        "uiTokenAmount": {"amount": str(amount), "decimals": 0}}]
    return {"transaction": {"signatures": ["signature"], "message": {"accountKeys": [owner]}},
            "meta": {"err": None, "preBalances": [pre], "postBalances": [pre + sol_delta],
                "preTokenBalances": before, "postTokenBalances": after}}


class WalletCopyTests(unittest.TestCase):
    def test_qualifying_wallet_signal_is_only_paper(self):
        state = empty_state()
        state["wallets"]["trader"] = {"closed_trades": 4, "wins": 3,
            "realized_lamports": 200_000_000, "total_hold_seconds": 1600, "long_holds": 4}
        process_confirmed_block(state, {"blockTime": 1000,
            "transactions": [tx("trader", "TOKEN", 100, -1_000_000_000)]})
        found = process_confirmed_block(state, {"blockTime": 1301, "transactions": []}, slot=10)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "paper_copy_opportunity")
        self.assertEqual(found[0]["source_held_seconds"], 301)
        self.assertEqual(found[0]["wallet_win_rate"], 0.75)

    def test_wallet_must_have_closed_profitable_history(self):
        state = empty_state()
        state["wallets"]["trader"] = {"closed_trades": 2, "wins": 2,
            "realized_lamports": 200_000_000, "total_hold_seconds": 1000, "long_holds": 2}
        self.assertEqual(best_wallets(state), [])

    def test_auto_follow_requires_profitable_history_and_five_minute_median(self):
        state = {"enabled": False, "real": False, "wallets": {}, "balance": 100.0,
                 "start": 100.0, "positions": {}, "history": []}
        candidate = {"wallet": "11111111111111111111111111111111", "closed_trades": 3,
            "realized_lamports": 100, "hold_samples": [360, 420, 600], "average_buy_lamports": 10**9, "age_days": 60, "active_days_7": 7}
        with patch("copy_trader._load", return_value=state), patch("copy_trader._save"), \
                patch("copy_trader.wallet_usd", return_value=1000.0), patch("copy_trader._sol_usd", return_value=200.0):
            self.assertTrue(copy_trader.auto_follow(candidate))
            self.assertTrue(state["wallets"][candidate["wallet"]]["auto_discovered"])

    def test_auto_follow_rejects_wallets_that_sell_too_fast(self):
        candidate = {"wallet": "11111111111111111111111111111111", "closed_trades": 3,
            "realized_lamports": 100, "hold_samples": [100, 200, 600]}
        with patch("copy_trader._load") as load, patch("copy_trader._save"):
            self.assertFalse(copy_trader.auto_follow(candidate))
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
