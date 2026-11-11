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


class FastTraderTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        d = ct._load()
        d["score_v2"] = True
        d["wallets"][W] = {"last_sig": "s", "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False, "holds": [],
                           "open_buys": {}, "followed_at": time.time()}
        ct._save(d)

    def trade(self, mint, minutes, t0):
        ct._track_hold(W, "buy", mint, t0)
        return ct._track_hold(W, "sell", mint, t0 + minutes * 60)

    def test_one_quick_trade_does_not_pause(self):
        self.assertIsNone(self.trade("A", 2, 1000))
        self.assertIsNone(self.trade("B", 30, 5000))
        self.assertFalse(ct._load()["wallets"][W]["paused"])

    def test_two_trades_under_5_min_pause_even_in_test(self):
        self.trade("A", 2, 1000)
        msg = self.trade("B", 3, 5000)
        self.assertIn("trades de menos de 5 min", msg)
        w = ct._load()["wallets"][W]
        self.assertTrue(w["paused"])
        self.assertEqual(ct.unpause_test_wallets(), [])          # not undone by the test-period unpause
        self.assertIn("pausada (trades de menos de 5 min)", ct.wallets_text())

    def test_paused_fast_wallet_still_sells_open_copies(self):
        d = ct._load()
        d["wallets"][W].update(paused=True, pause_reason="fast")
        d["positions"][MINT] = {"wallet": W, "real": False, "tokens": 10, "cost_usd": 10.0, "opened_at": time.time()}
        d["enabled"] = True
        ct._save(d)
        with patch.object(ct, "new_signals", return_value=[("sell", MINT, 1.0)]), \
                patch.object(ct, "_close", return_value=[]) as close, patch.object(ct, "users_active", return_value=False):
            ct.cycle()
        close.assert_called_once()


class ManualPauseTests(unittest.TestCase):
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

    def tap(self, data):
        import telegram_bot as tb
        sent = []
        tb.CTX.interactive = True
        with patch.object(tb, "authorized", return_value=True), patch.object(tb, "request_api"), \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append((t, kb))):
            tb.handle({"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": 1, "type": "private"}}}})
        return sent

    def test_owner_pauses_and_resumes_a_wallet_with_buttons(self):
        buttons = ct.manage_buttons()
        self.assertEqual(buttons[0][2]["callback_data"], "cw_off:" + W)
        self.assertLessEqual(len(buttons[0][2]["callback_data"].encode()), 64)          # Telegram limit
        sent = self.tap("cw_off:" + W)
        self.assertIn("Pausei", sent[-2][0])
        self.assertIn("⏸️ pausada (por você)", sent[-1][0])           # updated list sent right away
        w = ct._load()["wallets"][W]
        self.assertEqual((w["paused"], w["pause_reason"]), (True, "manual"))
        self.assertEqual(ct.unpause_test_wallets(), [])                                 # never undone by the bot
        self.assertIn("pausada (por você)", ct.wallets_text())
        self.assertEqual(ct.manage_buttons()[0][2]["callback_data"], "cw_on:" + W)
        sent = self.tap("cw_on:" + W)
        self.assertFalse(ct._load()["wallets"][W]["paused"])
        self.assertIn("Status agora: ✅ COPIANDO", sent[-2][0])
        self.assertIn("1. ✅ copiando", sent[-1][0])                    # the list sent again shows it copying
        self.assertNotIn("pausada (", sent[-1][0])


class OrphanExitTests(unittest.TestCase):
    def test_copy_without_origin_wallet_stop_40_and_trailing_from_25(self):
        from live_exit_monitor import position_signal
        pos = {"mode": "copy", "copy_orphan": True, "opened_at": time.time()}
        self.assertEqual(position_signal(pos, 32, 32 * 0.86)["action"], "hold")     # -14%: no longer sold at -15%
        self.assertEqual(position_signal(pos, 32, 32 * 0.61)["action"], "hold")     # -39%
        self.assertEqual(position_signal(pos, 32, 32 * 0.59)["reason"], "stop_loss")  # -41%
        self.assertEqual(position_signal(pos, 32, 32 * 1.32)["reason"], "trailing")   # +32%: trailing on, holds
        top = dict(pos, peak_ratio=1.60)
        self.assertEqual(position_signal(top, 32, 32 * 1.50)["action"], "hold")      # 6% below the top
        self.assertEqual(position_signal(top, 32, 32 * 1.40)["reason"], "take_profit")  # 12.5% below: sells


class ProportionalTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"), "COPY_PROPORTIONAL_WALLETS": W})
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(ct, "_sol_usd", return_value=100.0)
        q.start()
        self.addCleanup(q.stop)
        d = ct._load()
        d["score_v2"] = True
        d["enabled"] = True
        d["wallets"][W] = {"last_sig": "s", "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False,
                           "holds": [600, 700, 900], "open_buys": {}, "followed_at": time.time()}
        ct._save(d)

    def test_fraction_of_the_wallet_balance_used_in_a_buy(self):
        tx = {"meta": {"err": None, "preBalances": [10_000_000_000], "postBalances": [7_000_000_000],
                       "preTokenBalances": [], "postTokenBalances": [
                           {"owner": W, "mint": MINT, "uiTokenAmount": {"amount": "500", "decimals": 6}}]},
              "transaction": {"message": {"accountKeys": [W]}}}
        self.assertAlmostEqual(ct.buy_fraction(tx, W), 0.30)          # spent 3 of 10 SOL

    def test_between_10_and_30_percent_of_our_wallet(self):
        self.assertEqual(ct.proportional_usd(0.25, 1000), 250.0)        # 25% -> 25% (no dollar cap)
        self.assertEqual(ct.proportional_usd(0.02, 1000), 100.0)        # 2% -> at least 10%
        self.assertEqual(ct.proportional_usd(0.90, 1000), 300.0)        # 90% -> at most 30%
        self.assertEqual(ct.proportional_usd(0.30, 1000, free_usd=150), 148.0)   # only what is free (- fees)
        self.assertIsNone(ct.proportional_usd(0.30, 1000, free_usd=3))   # nothing free

    def test_proportional_wallet_buys_same_share_even_small_buys(self):
        ct.BUY_FRACTION[(W, MINT)] = 0.10
        with patch.object(ct, "new_signals", return_value=[("buy", MINT, 12.0)]), \
                patch.object(ct, "users_active", return_value=False), \
                patch.object(ct, "_open", return_value=None) as buy:
            ct.cycle()
        self.assertEqual(buy.call_args.kwargs["usd"], 10.0)            # 10% of the US$ 100 simulation wallet
        self.assertIn("📐", ct.wallets_text())

    def test_mirror_wallet_copies_even_a_fast_trader_without_audit(self):
        d = ct._load()
        d["wallets"][W].update(holds=[30, 40, 50], followed_at=time.time() - 10 * 86400)   # fast, test over, no audit
        ct._save(d)
        ct.BUY_FRACTION[(W, MINT)] = 0.2
        with patch.object(ct, "new_signals", return_value=[("buy", MINT, 1.0)]), \
                patch.object(ct, "users_active", return_value=False), \
                patch.object(ct, "_open", return_value=None) as buy:
            ct.cycle()
        buy.assert_called_once()

    def test_mirror_wallet_is_never_paused_or_removed_automatically(self):
        d = ct._load()
        for _ in range(10):
            ct._score(d, W, -50.0)
        self.assertFalse(d["wallets"][W]["paused"])
        d["wallets"][W]["followed_at"] = time.time() - 10 * 86400                    # test over, no profit
        ct._save(d)
        self.assertEqual(ct.remove_fast_wallets(), [])
        self.assertIn(W, ct._load()["wallets"])
        ct.set_paused(W, True)                                                       # the owner can still pause it
        self.assertEqual(ct.unpause_test_wallets(), [])
        d = ct._load()
        d["wallets"][W]["pause_reason"] = "fast"                                     # paused before by an automatic rule
        ct._save(d)
        self.assertEqual(ct.unpause_test_wallets(), [W])

    def test_buying_the_same_coin_again_stays_under_30_percent(self):
        self.assertEqual(ct.add_on_room(250, 200, 1000), 100.0)        # 20% + 25% -> only up to 30%
        self.assertIsNone(ct.add_on_room(300, 300, 1000))              # already 30%: skip
        self.assertEqual(ct.add_on_room(300, 0, 1000), 300)            # first buy: 10%-30% rule only

    def test_cycle_cuts_the_rebuy_to_30_percent(self):
        d = ct._load()
        d["positions"][MINT] = {"wallet": W, "real": False, "tokens": 1, "cost_usd": 25.0, "opened_at": time.time()}
        d["balance"] = 75.0                                            # wallet value = 75 free + 25 in the coin
        ct._save(d)
        ct.BUY_FRACTION[(W, MINT)] = 0.30
        with patch.object(ct, "new_signals", return_value=[("buy", MINT, 12.0)]), \
                patch.object(ct, "users_active", return_value=False), \
                patch.object(ct, "_open", return_value=None) as buy:
            ct.cycle()
        self.assertEqual(buy.call_args.kwargs["usd"], 5.0)             # 30% of 100 = 30, already 25 -> 5

    def test_sim_copy_uses_the_custom_size(self):
        with patch.object(ct, "safety_reason", return_value=""), \
                patch.object(ct, "_order", return_value={"outAmount": "1000"}):
            ct._open(MINT, W, None, None, usd=25.0)
        self.assertAlmostEqual(ct._load()["positions"][MINT]["cost_usd"], 25.0 + ct.NETWORK_FEE_USD)


class RepeatBuyTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(ct, "_sol_usd", return_value=100.0)
        q.start()
        self.addCleanup(q.stop)
        d = ct._load()
        d["score_v2"] = True
        d["wallets"][W] = {"last_sig": "s", "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False, "holds": []}
        ct._save(d)

    def test_same_wallet_buying_the_same_coin_again_is_copied_again(self):
        with patch.object(ct, "safety_reason", return_value=""), patch.object(ct, "_order", return_value={"outAmount": "1000"}):
            ct._open(MINT, W, None, None)
            msg = ct._open(MINT, W, None, None)                # 2nd buy of the same coin
            ct._open(MINT, W, None, None)                      # 3rd
        p = ct._load()["positions"][MINT]
        self.assertIn("2ª compra da mesma moeda", msg)
        self.assertEqual((p["tokens"], p["buys"]), (3000, 3))
        self.assertAlmostEqual(p["cost_usd"], 3 * (ct.ORDER_USD + ct.NETWORK_FEE_USD))
        self.assertIn("3 compras", ct.positions_text())

    def test_limit_of_buys_per_coin(self):
        with patch.object(ct, "safety_reason", return_value=""), patch.object(ct, "_order", return_value={"outAmount": "1000"}):
            for _ in range(8):
                ct._open(MINT, W, None, None, usd=5.0)
        self.assertEqual(ct._load()["positions"][MINT]["buys"], ct.MAX_BUYS_PER_COIN)

    def test_other_wallet_buying_a_coin_we_hold_is_not_added(self):
        other = "Other" + "x" * 39
        with patch.object(ct, "safety_reason", return_value=""), patch.object(ct, "_order", return_value={"outAmount": "1000"}):
            ct._open(MINT, W, None, None)
            self.assertIsNone(ct._open(MINT, other, None, None))
        self.assertEqual(ct._load()["positions"][MINT].get("buys", 1), 1)

    def test_live_engine_adds_a_new_buy_to_the_position(self):
        import live_trade_state as st
        d = st._load()
        d.update(enabled=True, halted=False)
        st._save(d)
        st.reserve("o1", MINT, "buy")
        st.record_settlement("o1", "sig1", position={"token_amount_raw": 100, "cost_usd": 32.0, "buy_signature": "sig1"})
        st.set_position_fields(MINT, mode="copy", copy_wallet=W)
        st.reserve("o2", MINT, "buy")
        st.record_settlement("o2", "sig2", position={"token_amount_raw": 50, "cost_usd": 20.0, "buy_signature": "sig2"})
        p = st._load()["positions"][MINT]
        self.assertEqual((p["token_amount_raw"], p["cost_usd"], p["buys"], p["copy_wallet"]), (150, 52.0, 2, W))

    def test_buying_more_of_a_held_coin_doesnt_need_a_free_slot(self):
        import live_trade_state as st
        d = st._load()
        d.update(enabled=True, halted=False)
        d["positions"] = {"M" + str(i): {"token_amount_raw": 1, "cost_usd": 1} for i in range(st.MAX_POSITIONS - 1)}
        d["positions"][MINT] = {"token_amount_raw": 1, "cost_usd": 1}
        st._save(d)
        self.assertFalse(st.can_open()[0])
        self.assertTrue(st.can_open(MINT)[0])


class PartialSellTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        import live_trade_state as st
        self.st = st
        s = st._load()
        s.update(enabled=True, halted=False)
        s["positions"][MINT] = {"token_amount_raw": 1000, "cost_usd": 40.0, "mode": "copy", "copy_wallet": W,
                                "symbol": "VEYL", "opened_at": time.time()}
        s["pending"]["o1"] = {"mint": MINT, "side": "sell", "signature": "sig", "before_token_raw": 1000,
                              "before_sol_raw": 1_000_000_000, "input_amount_raw": 300,
                              "created_at": "2026-11-11T00:00:00+00:00"}
        st._save(s)

    def test_settling_a_partial_sale_keeps_the_rest_open(self):
        st = self.st
        tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "OWNER", "signer": True}]}}, "meta": {"err": None}}
        with patch("live_swap_executor.rpc", return_value=tx), \
                patch("solana_settlement.get_token_balance", return_value=700), \
                patch("solana_settlement.get_sol_balance", return_value=1_200_000_000), \
                patch.object(st, "_sol_to_usd", return_value=18.0):
            r = st.reconcile_known_signature("o1", "OWNER")
        self.assertTrue(r["partial"])
        self.assertAlmostEqual(r["cost_usd"], 12.0)                  # 30% of US$ 40
        self.assertAlmostEqual(r["pnl_usd"], 6.0)
        left = st._load()["positions"][MINT]
        self.assertEqual(left["token_amount_raw"], 700)
        self.assertAlmostEqual(left["cost_usd"], 28.0)
        self.assertEqual(left["copy_wallet"], W)                     # still sold together with the wallet later

    def test_bridge_accepts_partial_sale_and_buy_more_only_when_asked(self):
        import live_order_bridge as b
        st = self.st
        s = st._load()
        s["pending"] = {}
        st._save(s)
        with patch.object(b, "preflight"), patch("bot_wallet_service.get_address", return_value="W"):
            with self.assertRaises(b.TradingNotReady):
                b.submit_swap(MINT, "sell", 300, "W")                   # whole position required
            with self.assertRaises(b.TradingNotReady):
                b.submit_swap(MINT, "buy", 10_000_000, "W")             # already open
            with patch.object(b, "sol_for_usd", side_effect=RuntimeError("stop here")):
                with self.assertRaises(RuntimeError):
                    b.submit_swap(MINT, "buy", 10_000_000, "W", add_on=True)   # passes the checks
            with patch.object(st, "reserve", side_effect=RuntimeError("stop here")):
                with self.assertRaises(RuntimeError):
                    b.submit_swap(MINT, "sell", 300, "W", partial=True)        # passes the checks

    def test_copy_sells_the_same_part_in_real(self):
        d = ct._load()
        d["score_v2"] = True
        d["enabled"] = True
        d["wallets"][W] = {"last_sig": "s", "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False, "holds": []}
        d["positions"][MINT] = {"wallet": W, "real": True, "opened_at": time.time()}
        ct._save(d)
        import live_engine
        with patch.object(ct, "new_signals", return_value=[("sell", MINT, 0.3)]), \
                patch.object(ct, "users_active", return_value=False), \
                patch.object(live_engine, "sell_part", return_value="sold") as part, \
                patch.object(ct, "_close") as close:
            ct.cycle("send", 1)
        self.assertEqual(part.call_args.args[:2], (MINT, 0.3))
        close.assert_not_called()                                    # the rest stays open
        self.assertIn(MINT, ct._load()["positions"])


class ResumeProportionalTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"), "COPY_PROPORTIONAL_WALLETS": ""})
        p.start()
        self.addCleanup(p.stop)
        d = ct._load()
        d["score_v2"] = True
        d["wallets"][W] = {"last_sig": "s", "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": True, "holds": [],
                           "pause_reason": "manual", "followed_at": time.time()}
        ct._save(d)

    def test_resumed_wallet_copies_its_percentage_but_keeps_the_filters(self):
        ct.set_paused(W, False)
        w = ct._load()["wallets"][W]
        self.assertTrue(ct.proportional(W, w))                    # same % it puts in each coin
        self.assertFalse(ct.mirror(W, w))                         # not a mirror: filters and pauses still apply
        self.assertIn("📐 copia a % dela", ct.wallets_text())
        d = ct._load()
        for _ in range(6):
            ct._score(d, W, -1.0)
        self.assertTrue(d["wallets"][W]["paused"])                # 6 losses in a row still pause it


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
