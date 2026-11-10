import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
import copy_trader as ct
import telegram_bot as tb
import user_copy as uc
import user_wallets as uw

LEADER = "7HtLL1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MINT = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class UserCopyTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "wallet.json"), "TELEGRAM_CHAT_ID": "100",
                                    "BOT_WALLET_ENCRYPTION_PASSPHRASE": "x" * 32})
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(ct, "_sol_usd", return_value=100.0)
        q.start()
        self.addCleanup(q.stop)
        self.owner_wallet, _ = uw.create_or_get(100)
        self.sent = []
        self.send = lambda chat, text: self.sent.append((chat, text))

    def test_real_needs_fee_accepted_and_owner_is_not_a_user(self):
        with self.assertRaises(ValueError):
            uc.set_mode(200, "real")
        uc.accept_terms(200)
        uc.set_mode(200, "real")
        self.assertEqual(uc.load(200)["mode"], "real")
        with self.assertRaises(ValueError):
            uc.set_mode(100, "sim")

    def test_sim_buy_and_sell_take_4_percent(self):
        uc.set_mode(201, "sim")
        uc.set_order_usd(201, 10)
        with patch.object(ct, "_order", return_value={"outAmount": "1000"}) as order:
            uc.on_buy(MINT, LEADER, self.send, sync=True)
        self.assertEqual(int(order.call_args[0][2]), int(0.1e9 * 0.96))      # swaps 96% of US$ 10
        st = uc.load(201)
        self.assertAlmostEqual(st["sim_balance"], 90.0)
        self.assertAlmostEqual(st["sim_fees_usd"], 0.4)
        with patch.object(ct, "_order", return_value={"outAmount": str(int(0.2e9))}):      # sold for US$ 20
            uc.on_sell(MINT, LEADER, 1.0, self.send, sync=True)
        st = uc.load(201)
        self.assertEqual(st["positions"], {})
        self.assertAlmostEqual(st["history"][0]["pnl_usd"], 20 * 0.96 - 10, places=4)
        self.assertTrue(any("LUCRO" in t for _, t in self.sent))

    def test_real_buy_pays_fee_to_owner_wallet_after_swap(self):
        uc.accept_terms(202)
        uc.set_mode(202, "real")
        calls = []
        with patch.object(uc, "sol_balance", return_value=int(1e9)), \
                patch.object(uc, "quote_order", return_value={"requestId": "r"}), \
                patch.object(uc, "execute", side_effect=lambda c, od: calls.append("swap") or
                             {"signature": "s", "total_output_amount": 5000}), \
                patch.object(uc, "transfer_sol", side_effect=lambda kp, to, lam: calls.append(("fee", to, lam)) or "f"):
            uc.on_buy(MINT, LEADER, self.send, sync=True)
        self.assertEqual(calls[0], "swap")                                   # fee only after the trade went through
        self.assertEqual(calls[1], ("fee", self.owner_wallet, int(0.1e9 * 0.04)))
        st = uc.load(202)
        self.assertEqual(st["positions"][MINT]["tokens"], 5000)
        self.assertEqual(st["fees_paid_lamports"], int(0.1e9 * 0.04))

    def test_failed_quote_charges_nothing(self):
        uc.accept_terms(203)
        uc.set_mode(203, "real")
        with patch.object(uc, "sol_balance", return_value=int(1e9)), \
                patch.object(uc, "quote_order", side_effect=RuntimeError("no route")), \
                patch.object(uc, "transfer_sol") as fee:
            uc.on_buy(MINT, LEADER, self.send, sync=True)
        fee.assert_not_called()
        self.assertEqual(uc.load(203)["positions"], {})

    def test_low_balance_does_not_buy(self):
        uc.accept_terms(204)
        uc.set_mode(204, "real")
        with patch.object(uc, "sol_balance", return_value=1000), patch.object(uc, "quote_order") as q:
            uc.on_buy(MINT, LEADER, self.send, sync=True)
        q.assert_not_called()
        self.assertIn("Saldo insuficiente", self.sent[-1][1])

    def test_failed_fee_is_owed_and_retried(self):
        uc.accept_terms(205)
        uc.set_mode(205, "real")
        with patch.object(uc, "transfer_sol", side_effect=TimeoutError("x")):
            uc._charge_fee(205, 1234)
        self.assertEqual(uc.load(205)["fees_owed_lamports"], 1234)
        with patch.object(uc, "transfer_sol", return_value="ok"):
            uc.upkeep()
        st = uc.load(205)
        self.assertEqual((st["fees_owed_lamports"], st["fees_paid_lamports"]), (0, 1234))

    def test_real_sell_pays_fee_on_proceeds(self):
        d = uc._default()
        d.update(mode="real", terms_at=1)
        d["positions"][MINT] = {"leader": LEADER, "real": True, "tokens": 5000, "cost_usd": 10.0, "opened_at": time.time()}
        uc.save(206, d)
        uw.create_or_get(206)
        fees = []
        with patch.object(uc, "token_balance", return_value=5000), \
                patch.object(uc, "swap", return_value={"signature": "s", "total_output_amount": int(0.05e9)}), \
                patch.object(uc, "transfer_sol", side_effect=lambda kp, to, lam: fees.append(lam) or "f"):
            uc.on_sell(MINT, LEADER, 1.0, self.send, sync=True)
        self.assertEqual(fees, [int(0.05e9 * 0.04)])
        self.assertAlmostEqual(uc.load(206)["history"][0]["pnl_usd"], 5 * 0.96 - 10, places=4)

    def test_wallet_with_user_position_is_not_removed(self):
        d = uc._default()
        d["positions"][MINT] = {"leader": LEADER, "real": False, "tokens": 1, "cost_usd": 10.0}
        uc.save(207, d)
        ct.follow(LEADER)
        self.assertFalse(ct.unfollow(LEADER))           # stays (leaving) while a user holds a copy from it

    def test_cycle_sends_signals_to_users_when_owner_copy_is_off(self):
        uc.set_mode(208, "sim")
        ct.follow(LEADER)
        d = ct._load()
        d["wallets"][LEADER].update(holds=[600, 700, 900], followed_at=time.time())
        ct._save(d)
        with patch.object(ct, "new_signals", return_value=[("buy", MINT, 200.0)]), \
                patch.object(ct, "safety_reason", return_value=""), patch.object(ct, "_open") as owner_open, \
                patch("user_copy.on_buy") as users_buy:
            ct.cycle()
        users_buy.assert_called_once()
        owner_open.assert_not_called()                  # owner's copy is off

    def test_user_menu_and_fee_terms(self):
        sent = []
        with patch.object(tb, "request_api"), patch.object(tb, "CHAT", "100"), \
                patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append((t, kb))):
            tb.handle({"callback_query": {"id": "1", "data": "u_real", "message": {"chat": {"id": 300, "type": "private"}}}})
            self.assertIn("TAXA: 4%", sent[-1][0])
            self.assertEqual(uc.load(300)["mode"], "off")              # not on before accepting
            tb.handle({"callback_query": {"id": "1", "data": "u_real_ok", "message": {"chat": {"id": 300, "type": "private"}}}})
        self.assertEqual(uc.load(300)["mode"], "real")
        self.assertIn("u_sim", {b["callback_data"] for row in tb.user_keyboard() for b in row})


class FeeTransferTests(unittest.TestCase):
    def test_transfer_is_signed_by_user_and_pays_owner(self):
        import base64
        from solders.keypair import Keypair
        from solders.transaction import Transaction
        user, owner = Keypair(), Keypair()
        sent = {}

        def rpc(method, params):
            if method == "getLatestBlockhash":
                return {"value": {"blockhash": "11111111111111111111111111111111"}}
            if method == "sendTransaction":
                sent["tx"] = Transaction.from_bytes(base64.b64decode(params[0]))
                return "sig"
            return {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        with patch.object(uc, "_rpc", side_effect=rpc):
            self.assertEqual(uc.transfer_sol(user, str(owner.pubkey()), 4_000_000), "sig")
        tx = sent["tx"]
        self.assertTrue(tx.verify_with_results()[0])
        keys = [str(k) for k in tx.message.account_keys]
        self.assertEqual(keys[0], str(user.pubkey()))            # the user pays
        self.assertIn(str(owner.pubkey()), keys)                  # the owner receives
        data = bytes(tx.message.instructions[0].data)
        self.assertEqual(int.from_bytes(data[4:12], "little"), 4_000_000)


if __name__ == "__main__":
    unittest.main()
