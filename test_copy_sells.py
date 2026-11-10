"""The owner's copy must be sold when the copied wallet sells, even if the first attempt fails or the
sell signal was missed."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import copy_trader as ct
import user_copy as uc

W = "7HtLL1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MINT = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class CopySellTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"), "TELEGRAM_CHAT_ID": "100",
                                    "BOT_WALLET_ENCRYPTION_PASSPHRASE": "x" * 32})
        p.start()
        self.addCleanup(p.stop)
        ct._WATCH["at"] = 0.0
        d = ct._load()
        d["wallets"][W] = {"last_sig": "s", "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False, "holds": []}
        d["positions"][MINT] = {"wallet": W, "real": True, "opened_at": time.time() - 600}
        ct._save(d)

    def test_failed_sell_is_retried_until_it_works(self):
        import live_engine
        import live_trade_state as st
        with patch.object(live_engine, "sell_now", side_effect=RuntimeError("Jupiter returned no valid route")), \
                patch.object(st, "pnl_summary", return_value={"total_pnl_usd": 0.0}):
            msgs = ct._close(MINT, "copy_follow")
        self.assertIn("vou tentar de novo", msgs[0].lower())
        self.assertTrue(ct._load()["positions"][MINT]["sell_wanted"])           # not forgotten
        d = ct._load()
        d["positions"][MINT]["sell_tried"] = 0
        ct._save(d)
        with patch.object(live_engine, "sell_now", return_value="sold"), \
                patch.object(st, "pnl_summary", return_value={"total_pnl_usd": 0.0}), \
                patch.object(st, "history", return_value=[], create=True):
            ct.retry_wanted_sells()
        self.assertNotIn(MINT, ct._load()["positions"])

    def test_missed_sell_is_caught_by_the_wallet_balance(self):
        with patch.object(ct, "wallet_token_balance", return_value=0), patch.object(ct, "_close", return_value=[]) as close:
            ct.watch_copied_balances()
        close.assert_called_once_with(MINT, "copy_follow", None, None)

    def test_wallet_still_holding_keeps_the_copy(self):
        with patch.object(ct, "wallet_token_balance", return_value=5), patch.object(ct, "_close") as close:
            ct.watch_copied_balances()
        close.assert_not_called()

    def test_busy_wallet_reads_more_than_15_transactions(self):
        page1 = [{"signature": "n" + str(i), "err": None} for i in range(50)]
        page2 = [{"signature": "m" + str(i), "err": None} for i in range(10)] + [{"signature": "OLD", "err": None}]
        calls = []

        def rpc(method, params):
            if method == "getSignaturesForAddress":
                calls.append(params[1])
                return page1 if "before" not in params[1] else page2
            return {"meta": {"err": None}}
        with patch.object(ct, "_rpc", side_effect=rpc):
            ct.new_signals(W, {"last_sig": "OLD"})
        self.assertEqual(calls[1]["before"], "n49")                            # went back to the older page

    def test_transaction_not_available_yet_is_read_again(self):
        def rpc(method, params):
            if method == "getSignaturesForAddress":
                return [{"signature": "NEW", "err": None}, {"signature": "OLD", "err": None}]
            return None
        with patch.object(ct, "_rpc", side_effect=rpc):
            with self.assertRaises(RuntimeError):
                ct.new_signals(W, {"last_sig": "OLD"})        # cycle keeps last_sig: the sell is not lost

    def test_user_copy_sold_when_copied_wallet_no_longer_holds(self):
        d = uc._default()
        d["positions"][MINT] = {"leader": W, "real": False, "tokens": 10, "cost_usd": 10.0,
                                "opened_at": time.time() - 600}
        uc.save(300, d)
        with patch.object(uc, "leader_balance", return_value=0), patch.object(uc, "_sell") as sell:
            uc.watch_leaders()
        sell.assert_called_once_with("300", MINT, 1.0, None)


class MonitorTests(unittest.TestCase):
    def test_copy_position_is_monitored_without_error(self):
        import live_exit_monitor as m
        pos = {"mode": "copy", "token_amount_raw": 100, "cost_usd": 10.0, "opened_at": time.time()}
        with patch.object(m, "_load", return_value={"positions": {MINT: pos}, "pending": {}}), \
                patch.object(m, "quote_value_usd", return_value=12.0), patch.object(m.live_trade_state, "update_peak"), \
                patch.object(m, "_jupiter_healthy") as healthy:
            out = list(m._evaluate())
        healthy.assert_not_called()                                   # no "failure" path, never written off as dead
        self.assertEqual(out[0][2]["action"], "hold")
        self.assertAlmostEqual(out[0][2]["peak_ratio"], 1.2)


if __name__ == "__main__":
    unittest.main()
