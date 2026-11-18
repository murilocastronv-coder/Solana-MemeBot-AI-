import tempfile
import unittest
from unittest import mock

import user_copy as uc
import user_trade as ut

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


class UserTradeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = mock.patch.object(ut.user_wallets, "_base_dir", return_value=__import__("pathlib").Path(self.dir))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_fee_is_two_percent(self):
        self.assertEqual(ut.FEE_RATE, 0.02)

    def test_buy_needs_terms_and_takes_fee_inside_the_swap(self):
        self.assertIn("Aceite", ut.buy(5, MINT, 0.5))
        ut.accept_terms(5)
        bal = {"tok": 0}
        calls = []

        def swap(chat, i, o, amount, fee_lamports=None, **k):
            calls.append((amount, fee_lamports, k.get("fee_rate")))
            bal["tok"] = 1000
            return {"fee_lamports": fee_lamports}
        with mock.patch.object(uc, "_keypair", return_value=("ADDR", None)), \
                mock.patch.object(uc, "sol_balance", return_value=int(2e9)), \
                mock.patch.object(uc, "token_balance", side_effect=lambda a, m: bal["tok"]), \
                mock.patch.object(uc, "atomic_swap", side_effect=swap), mock.patch.object(ut, "sol_usd", return_value=100.0):
            msg = ut.buy(5, MINT, 0.5, symbol="POP")
        self.assertIn("COMPROU POP", msg)
        self.assertEqual(calls, [(int(0.49e9), int(0.01e9), 0.02)])       # 0.5 SOL: 0.49 swapped + 0.01 fee
        p = ut.load(5)["positions"][MINT]
        self.assertEqual((p["tokens"], p["cost_usd"]), (1000, 50.0))
        self.assertEqual(ut.load(5)["fees_paid_lamports"], int(0.01e9))

    def test_buy_refuses_without_balance(self):
        ut.accept_terms(5)
        with mock.patch.object(uc, "_keypair", return_value=("ADDR", None)), \
                mock.patch.object(uc, "sol_balance", return_value=int(0.1e9)):
            self.assertIn("Saldo insuficiente", ut.buy(5, MINT, 0.5))

    def test_exit_rules_and_limit_hit(self):
        self.assertEqual(ut.exit_reason({"sl": 0.3}, 0.6, 1.0), "sl")
        self.assertEqual(ut.exit_reason({"tp": 0.5}, 1.6, 1.6), "tp")
        self.assertEqual(ut.exit_reason({"trail": 0.2}, 1.1, 1.5), "trail")
        self.assertIsNone(ut.exit_reason({"trail": 0.2}, 1.3, 1.5))
        self.assertTrue(ut.limit_hit({"price": 1.0}, 0.9))
        self.assertFalse(ut.limit_hit({"price": 1.0}, 0))

    def test_upkeep_sells_at_stop_and_fills_limit(self):
        ut.update(5, lambda d: d["positions"].update({MINT: {"symbol": "POP", "tokens": 100, "cost_usd": 10.0, "sl": 0.3}}))
        ut.add_limit(5, "OTHER", "OTH", 2.0, 0.5, 0.1)                        # buy at US$ 1.0
        sold, bought = [], []
        ut.upkeep(lambda *a, **k: None, chats=["5"], value=lambda m, t: 6.0, prices=lambda ms: {"OTHER": 0.95},
                  buy_fn=lambda c, m, sol, s, symbol=None: bought.append((m, sol)) or "ok",
                  sell_fn=lambda c, m, pct, s, reason=None: sold.append((m, reason)) or "ok")
        self.assertEqual(sold, [(MINT, "sl")])
        self.assertEqual(bought, [("OTHER", 0.1)])
        self.assertEqual(ut.load(5)["limits"], {})


if __name__ == "__main__":
    unittest.main()
