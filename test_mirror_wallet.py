import os
import tempfile
import time
import unittest
from unittest.mock import patch

import live_exit_monitor
import live_trade_state as st
import mirror_wallet as mw

W = mw.DEFAULT_TARGET
MINT = "Coin111111111111111111111111111111111111111"


def swap_tx(wallet, mint, before, after, sol_change, t=None):
    """A swap where `wallet` goes from `before` to `after` tokens and its SOL moves by `sol_change`."""
    def bal(amount):
        return [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(int(amount * 1e6)), "decimals": 6}}]
    return {"blockTime": int(t or time.time()),
            "transaction": {"message": {"accountKeys": [wallet, "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]}},
            "meta": {"err": None, "preBalances": [100_000_000_000, 0],
                     "postBalances": [100_000_000_000 + int(sol_change * 1e9), 0],
                     "preTokenBalances": bal(before) if before else [], "postTokenBalances": bal(after) if after else []}}


class FakeChain:
    """Target wallet history served newest first, like getSignaturesForAddress."""
    def __init__(self):
        self.txs = []
        self.holds = None

    def add(self, tx):
        self.txs.append(("sig" + str(len(self.txs)), tx))

    def __call__(self, method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": s, "err": None} for s, _ in reversed(self.txs)] or [{"signature": "start", "err": None}]
        if method == "getTransaction":
            return dict(self.txs)[params[0]]
        if method == "getTokenAccountsByOwner":
            if self.holds is None:
                raise RuntimeError("unknown")
            return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": self.holds}}}}}}]}
        raise AssertionError(method)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        for p in (patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}),
                  patch.object(mw, "_sol_usd", return_value=200.0),
                  patch.object(mw, "ANTI_SCAM", False),
                  patch.object(mw, "SIZE_MODE", "fixo"),
                  patch.object(mw, "ORDER_USD", 5.0)):
            p.start()
            self.addCleanup(p.stop)
        # 1 SOL (US$ 200) buys 1,000,000 raw tokens and back
        q = patch.object(mw, "_order", side_effect=lambda i, o, a: {"outAmount": str(int(a))})
        q.start()
        self.addCleanup(q.stop)
        mw._last.update(safety=time.monotonic() + 1e9, wallet_check=time.monotonic() + 1e9)
        self.chain = FakeChain()
        mw.set_mode(True, False)
        mw.cycle(rpc=self.chain)          # first look: starts from now

    def test_reads_buy_and_sell_with_balances(self):
        buy = mw.trade_from_tx(swap_tx(W, MINT, 0, 1000, -1.0), W, 200.0)
        self.assertEqual((buy["side"], buy["before"], buy["after"]), ("buy", 0, 1000))
        self.assertAlmostEqual(buy["usd"], 200.0)
        sell = mw.trade_from_tx(swap_tx(W, MINT, 1000, 400, 0.8), W, 200.0)
        self.assertEqual(sell["side"], "sell")
        self.assertAlmostEqual(sell["tokens"], 600)

    def test_mirrors_entry_add_partial_and_full_exit(self):
        self.chain.add(swap_tx(W, MINT, 0, 1000, -1.0))
        msgs = mw.cycle(rpc=self.chain)
        pos = mw._load()["positions"][MINT]
        self.assertIn("comprou", msgs[0])
        self.assertAlmostEqual(pos["first_usd"], 5.0)
        first_tokens = pos["tokens"]

        self.chain.add(swap_tx(W, MINT, 1000, 1500, -0.5))      # adds half of its first buy
        self.assertIn("reforçou", mw.cycle(rpc=self.chain)[0])
        pos = mw._load()["positions"][MINT]
        self.assertAlmostEqual(pos["tokens"], first_tokens * 1.5, delta=2)

        self.chain.add(swap_tx(W, MINT, 1500, 750, 1.0))        # sells half
        self.assertIn("50%", mw.cycle(rpc=self.chain)[0])
        pos = mw._load()["positions"][MINT]
        self.assertAlmostEqual(pos["tokens"], first_tokens * 0.75, delta=2)

        self.chain.add(swap_tx(W, MINT, 750, 0, 1.0))           # sells the rest
        mw.cycle(rpc=self.chain)
        self.assertNotIn(MINT, mw._load()["positions"])
        self.assertEqual(len(mw._load()["history"]), 2)

    def test_ignores_late_buy(self):
        self.chain.add(swap_tx(W, MINT, 0, 1000, -1.0, t=time.time() - 600))
        self.assertEqual(mw.cycle(rpc=self.chain), [])
        self.assertNotIn(MINT, mw._load()["positions"])

    def test_sells_when_wallet_no_longer_holds(self):
        self.chain.add(swap_tx(W, MINT, 0, 1000, -1.0))
        mw.cycle(rpc=self.chain)
        self.chain.holds = 0
        mw._last["wallet_check"] = 0
        self.assertIn("não tem mais", mw.cycle(rpc=self.chain)[0])
        self.assertNotIn(MINT, mw._load()["positions"])

    def test_proportional_size(self):
        with patch.object(mw, "SIZE_MODE", "proporcional"):
            prof = {"size_median": 100.0}
            self.assertEqual(mw.order_size(100, prof), 5.0)
            self.assertEqual(mw.order_size(1000, prof), 10.0)   # capped at 2x
            self.assertEqual(mw.order_size(10, prof), 2.5)      # floor 0.5x

    def test_safety_exits_from_profile(self):
        ex = mw.exits_from_profile({"sl_p25": -0.4, "hold_p90": 30.0})
        self.assertAlmostEqual(ex["sl"], 0.44)
        self.assertIsNone(ex["tp"])
        self.assertEqual(ex["max_hold_s"], 3600)
        self.assertEqual(mw.exits_from_profile(None)["sl"], 0.5)


class RealMirrorPieces(unittest.TestCase):
    def test_exit_monitor_mirror_has_no_target_only_stop(self):
        pos = {"mode": "mirror", "copy_sl": 0.3, "copy_tp": None, "opened_at": time.time()}
        self.assertEqual(live_exit_monitor.position_signal(pos, 10, 50)["action"], "hold")   # +400%: keeps riding
        self.assertEqual(live_exit_monitor.position_signal(pos, 10, 6.9)["reason"], "stop_loss")

    def test_partial_sell_and_add_buy_settlement(self):
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}):
            s = st._load()
            s.update(enabled=True, halted=False)
            s["positions"][MINT] = {"token_amount_raw": 1000, "cost_usd": 10.0, "mode": "mirror", "opened_at": 1}
            s["pending"]["o1"] = {"mint": MINT, "side": "buy", "partial": True}
            st._save(s)
            merged = st._merged_buy(st._load(), st._load()["pending"]["o1"], 500, 5.0, "sigA")
            st.record_settlement("o1", "sigA", position=merged)
            pos = st._load()["positions"][MINT]
            self.assertEqual((pos["token_amount_raw"], pos["cost_usd"], pos["opened_at"]), (1500, 15.0, 1))

            s = st._load()
            s["pending"]["o2"] = {"mint": MINT, "side": "sell", "partial": True}
            st._save(s)
            remaining = dict(pos, token_amount_raw=750, cost_usd=7.5)
            st.record_settlement("o2", "sigB", pnl_usd=1.0, remaining=remaining, sold_cost_usd=7.5)
            s = st._load()
            self.assertEqual(s["positions"][MINT]["token_amount_raw"], 750)
            self.assertEqual(s["history"][-1]["cost_usd"], 7.5)

    def test_real_partial_sell_keeps_the_rest_open(self):
        import solana_settlement
        import live_swap_executor
        owner = "Owner11111111111111111111111111111111111111"
        tx = {"transaction": {"message": {"accountKeys": [{"pubkey": owner, "signer": True}]}}, "meta": {"err": None}}
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}), \
                patch.object(live_swap_executor, "rpc", return_value=tx), \
                patch.object(solana_settlement, "get_token_balance", return_value=600), \
                patch.object(solana_settlement, "get_sol_balance", return_value=2_000_000_000), \
                patch.object(st, "_sol_to_usd", return_value=6.0):
            s = st._load()
            s.update(enabled=True, halted=False)
            s["positions"][MINT] = {"token_amount_raw": 1000, "cost_usd": 10.0, "mode": "mirror", "opened_at": 1}
            s["pending"]["o3"] = {"mint": MINT, "side": "sell", "partial": True, "signature": "sigC",
                                  "before_token_raw": 1000, "before_sol_raw": 1_000_000_000, "input_amount_raw": 400}
            st._save(s)
            r = st.reconcile_known_signature("o3", owner)
            self.assertEqual(r["status"], "settled_sell")
            self.assertTrue(r["partial"])
            self.assertAlmostEqual(r["pnl_usd"], 2.0)          # sold 40% (cost 4) for 6
            pos = st._load()["positions"][MINT]
            self.assertEqual((pos["token_amount_raw"], pos["cost_usd"]), (600, 6.0))

    def test_add_buy_ignores_position_limit(self):
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}), \
                patch.object(st, "MAX_POSITIONS", 1):
            s = st._load()
            s.update(enabled=True, halted=False)
            s["positions"][MINT] = {"token_amount_raw": 1, "cost_usd": 1.0}
            st._save(s)
            self.assertFalse(st.can_open()[0])
            self.assertTrue(st.can_open(adding=True)[0])


if __name__ == "__main__":
    unittest.main()
