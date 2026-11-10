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

    def test_wallet_that_sold_almost_everything_counts_as_closed(self):
        with patch.object(ct, "wallet_token_balance", return_value=1000), patch.object(ct, "_close") as close:
            ct.watch_copied_balances()                  # first look: it holds 1000
        close.assert_not_called()
        ct._WATCH["at"] = 0.0
        with patch.object(ct, "wallet_token_balance", return_value=30), \
                patch.object(ct, "_close", return_value=[]) as close:
            ct.watch_copied_balances()                  # now 3% of what it had: closed
        close.assert_called_once()

    def test_sale_being_confirmed_is_not_sent_twice(self):
        d = ct._load()
        d["positions"][MINT]["selling_since"] = time.time()
        ct._save(d)
        with patch.object(ct, "wallet_token_balance", return_value=0), patch.object(ct, "_close") as close:
            ct.watch_copied_balances()
        close.assert_not_called()

    def test_panel_shows_how_much_the_copied_wallet_still_holds(self):
        with patch.object(ct, "wallet_token_balance", side_effect=[1000]):
            ct.watch_copied_balances()
        ct._WATCH["at"] = 0.0
        with patch.object(ct, "wallet_token_balance", return_value=600):
            ct.watch_copied_balances()
        self.assertIn("ainda tem 60% do que tinha", ct.positions_text())

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


class ScoreTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        d = ct._load()
        d["score_v2"] = True
        d["wallets"][W] = {"last_sig": "s", "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False, "holds": [],
                           "followed_at": time.time()}
        ct._save(d)

    def test_unconfirmed_buy_is_not_counted_as_a_copy(self):
        import live_trade_state as st
        d = ct._load()
        d["positions"][MINT] = {"wallet": W, "real": True, "pending_buy": True, "opened_at": time.time() - 2000}
        ct._save(d)
        old = [{"mint": MINT, "pnl_usd": -32.0, "closed_at": time.time() - 9000}]   # an old trade of the same coin
        with patch.object(st, "_load", return_value={"positions": {}}), patch.object(st, "history", return_value=old):
            ct.sync_real_closed()
        w = ct._load()["wallets"][W]
        self.assertEqual((w["copies"], w["pnl_usd"]), (0, 0.0))
        self.assertNotIn(MINT, ct._load()["positions"])

    def test_real_copy_uses_its_own_sale(self):
        import live_engine
        import live_trade_state as st
        opened = time.time() - 600
        d = ct._load()
        d["positions"][MINT] = {"wallet": W, "real": True, "opened_at": opened}
        ct._save(d)
        hist = [{"mint": MINT, "pnl_usd": -32.0, "closed_at": opened - 3600},       # old, ignored
                {"mint": MINT, "pnl_usd": 4.5, "closed_at": time.time()}]          # this copy's sale
        with patch.object(live_engine, "sell_now", return_value="sold"), patch.object(st, "history", return_value=hist):
            ct._close(MINT, "copy_follow")
        w = ct._load()["wallets"][W]
        self.assertEqual((w["copies"], w["wins"], w["pnl_usd"]), (1, 1, 4.5))

    def test_pending_sale_keeps_the_copy_until_confirmed(self):
        import live_engine
        d = ct._load()
        d["positions"][MINT] = {"wallet": W, "real": True, "opened_at": time.time() - 600}
        ct._save(d)
        with patch.object(live_engine, "sell_now", return_value="pending"):
            ct._close(MINT, "copy_follow")
        self.assertIn(MINT, ct._load()["positions"])
        self.assertEqual(ct._load()["wallets"][W]["copies"], 0)

    def test_wrong_old_counts_are_reset_once_with_a_new_test(self):
        d = ct._load()
        d.pop("score_v2")
        d["wallets"][W].update(copies=34, wins=0, pnl_usd=-1006.03, paused=True, followed_at=time.time() - 86400)
        ct._save(d)
        self.assertTrue(ct.reset_bad_scores_once())
        w = ct._load()["wallets"][W]
        self.assertEqual((w["copies"], w["pnl_usd"], w["paused"]), (0, 0.0, False))
        self.assertTrue(ct.in_test(w))
        self.assertFalse(ct.reset_bad_scores_once())                               # only once


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
