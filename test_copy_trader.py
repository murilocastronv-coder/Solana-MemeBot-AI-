import os
import tempfile
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import copy_trader as c

WALLET = str(Keypair().pubkey())
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def tx(token_change, sol_change, wallet=WALLET, mint=MINT, err=None):
    pre_tok, post_tok = 1000, 1000 + token_change
    return {"meta": {"err": err, "preBalances": [5_000_000_000], "postBalances": [5_000_000_000 + sol_change],
                     "preTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(pre_tok)}}],
                     "postTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(post_tok)}}]},
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}]}}}


class SignalTests(unittest.TestCase):
    def test_buy_and_sell_detected(self):
        self.assertEqual(c.signal_from_tx(tx(500, -1_000_000_000), WALLET), ("buy", MINT))
        self.assertEqual(c.signal_from_tx(tx(-500, 120_000_000), WALLET), ("sell", MINT))

    def test_ignores_failed_and_unrelated(self):
        self.assertIsNone(c.signal_from_tx(tx(500, -100_000_000, err={"x": 1}), WALLET))
        self.assertIsNone(c.signal_from_tx(tx(500, -1_000_000_000), str(Keypair().pubkey())))
        self.assertIsNone(c.signal_from_tx(tx(0, -5000), WALLET))   # just a fee


class CopyTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start(); self.addCleanup(p.stop)
        c._safety_cache.clear(); c.EVENTS.clear(); c.LAST_ERROR["text"] = ""
        self.sigs, self.txs = [], {}
        for x in [patch.object(c, "_sol_usd", return_value=200.0),
                  patch.object(c, "safety_reason", return_value=""),
                  patch.object(c, "_order", side_effect=lambda i, o, a: {"outAmount": "1000" if i == c.SOL_MINT else str(self.sell_sol)}),
                  patch.object(c, "_rpc", side_effect=self.rpc)]:
            x.start(); self.addCleanup(x.stop)
        c.follow(WALLET)
        self.sell_sol = int(10 / 200 * 1e9)      # flat price by default

    def make_holder(self, minutes=10, n=3):
        d = c._load(); d["wallets"][WALLET]["holds"] = [minutes * 60] * n; c._save(d)

    def rpc(self, method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": s, "err": None} for s in reversed(self.sigs)]
        if method == "getTransaction":
            return self.txs[params[0]]

    def push(self, sig, t):
        self.sigs.append(sig); self.txs[sig] = t

    def test_first_look_does_not_copy_old_trades(self):
        c.set_mode(True)
        self.push("old", tx(500, -1_000_000_000))
        self.assertEqual(c.cycle(), [])
        self.assertEqual(c._load()["positions"], {})

    def test_copy_buy_then_sell_with_wallet(self):
        self.make_holder()
        c.set_mode(True)
        self.push("s0", tx(0, -5000)); c.cycle()                      # baseline
        self.push("s1", tx(500, -1_000_000_000))
        msgs = c.cycle()
        self.assertIn("copiou compra", msgs[0])
        self.assertIn(MINT, c._load()["positions"])
        self.sell_sol = int(11 / 200 * 1e9)
        self.push("s2", tx(-1000, 120_000_000))            # sold everything
        msgs = c.cycle()
        self.assertTrue(any("a carteira vendeu" in m for m in msgs))
        w = c._load()["wallets"][WALLET]
        self.assertEqual((w["copies"], w["wins"]), (1, 1))

    def test_partial_sell_is_mirrored(self):
        self.make_holder()
        c.set_mode(True)
        self.push("s0", tx(0, -5000)); c.cycle()
        self.push("s1", tx(500, -1_000_000_000)); c.cycle()
        self.push("s2", tx(-500, 300_000_000))              # wallet sells 500 of its 1000 tokens = 50%
        msgs = c.cycle()
        self.assertTrue(any("vendeu 50%" in m for m in msgs))
        pos = c._load()["positions"][MINT]
        self.assertEqual(pos["tokens"], 500)               # our 1000 tokens -> 500 left
        self.push("s3", tx(-1000, 300_000_000))             # sells everything left
        msgs = c.cycle()
        self.assertNotIn(MINT, c._load()["positions"])

    def test_no_fixed_tp_sl_by_default(self):
        self.make_holder()
        c.set_mode(True)
        self.push("s0", tx(0, -5000)); c.cycle()
        self.push("s1", tx(500, -1_000_000_000)); c.cycle()
        self.sell_sol = int(5 / 200 * 1e9)                 # -50%: no stop, keeps holding
        c.cycle()
        self.assertIn(MINT, c._load()["positions"])

    def test_stop_and_target(self):
        p = patch.object(c, "EXIT_MODE", "tp_sl"); p.start(); self.addCleanup(p.stop)
        self.make_holder()
        c.set_mode(True)
        self.push("s0", tx(0, -5000)); c.cycle()
        self.push("s1", tx(500, -1_000_000_000)); c.cycle()
        self.sell_sol = int(8 / 200 * 1e9)                            # -20%
        self.assertTrue(any("stop" in m for m in c.cycle()))

    def test_losing_wallet_is_paused(self):
        d = c._load()
        for _ in range(4):
            c._score(d, WALLET, -1.0)
        self.assertIn("Parei de copiar", c._score(d, WALLET, -1.0))
        self.assertTrue(d["wallets"][WALLET]["paused"])

    def test_unsafe_token_not_copied(self):
        self.make_holder()
        c.set_mode(True)
        self.push("s0", tx(0, -5000)); c.cycle()
        with patch.object(c, "safety_reason", return_value="criador pode congelar carteiras"):
            self.push("s1", tx(500, -1_000_000_000)); c.cycle()
        self.assertEqual(c._load()["positions"], {})
        self.assertIn("congelar", c.status_text())

    def test_real_mode_uses_live_engine(self):
        self.make_holder()
        c.set_mode(True, real=True)
        self.push("s0", tx(0, -5000)); c.cycle(lambda *a: None, "1")
        self.push("s1", tx(500, -1_000_000_000))
        with patch("live_engine.ready", return_value=True), \
             patch("live_trade_state.can_open", return_value=(True, "Ready")), \
             patch("live_trade_state.positions_by_mode", return_value=0), \
             patch("live_trade_state._load", return_value={"positions": {}}), \
             patch("live_engine.get_address", return_value="W"), \
             patch("live_engine._execute_buy", return_value={"status": "settled_buy"}) as buy:
            c.cycle(lambda *a: None, "1")
        self.assertEqual(buy.call_args[0][-1], "copy")
        self.assertEqual(buy.call_args[1]["usd"], c.ORDER_USD)

    def test_fast_flipper_is_not_copied_and_holder_is(self):
        c.set_mode(True)
        self.push("s0", tx(0, -5000)); c.cycle()
        self.push("s1", tx(500, -1_000_000_000)); c.cycle()
        self.assertEqual(c._load()["positions"], {})                  # still watching
        self.assertIn("observando", c.status_text())
        self.make_holder(minutes=1)
        self.push("s2", tx(500, -100_000_000, mint="JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN")); c.cycle()
        self.assertEqual(c._load()["positions"], {})
        self.assertIn("não copia", c.status_text())
        self.make_holder(minutes=8)
        self.push("s3", tx(500, -1_000_000_000, mint="7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")); c.cycle()
        self.assertEqual(len(c._load()["positions"]), 1)

    def test_small_buy_is_ignored_big_buy_is_copied(self):
        self.make_holder()
        c.set_mode(True)
        self.push("s0", tx(0, -5000)); c.cycle()
        self.push("s1", tx(500, -150_000_000)); c.cycle()          # 0.15 SOL x $200 = $30 < $40
        self.assertEqual(c._load()["positions"], {})
        self.assertIn("compra pequena", c.status_text())
        self.push("s2", tx(500, -250_000_000, mint="7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")); c.cycle()  # $50
        self.assertEqual(len(c._load()["positions"]), 1)

    def test_min_buy_default_is_40(self):
        self.assertEqual(c.MIN_BUY_USD, 40)

    def test_buy_value_counts_sol_spent(self):
        self.assertAlmostEqual(c.buy_value_usd(tx(500, -1_000_000_000), WALLET), 200.0)
        self.assertIsNone(c.buy_value_usd(tx(-500, 1_000_000_000), WALLET))

    def test_hold_time_is_measured(self):
        c._track_hold(WALLET, "buy", MINT, 1000)
        c._track_hold(WALLET, "sell", MINT, 1000 + 420)
        self.assertEqual(c._load()["wallets"][WALLET]["holds"], [420])

    def test_invalid_address_rejected(self):
        with self.assertRaises(Exception):
            c.follow("not-an-address")


if __name__ == "__main__":
    unittest.main()
