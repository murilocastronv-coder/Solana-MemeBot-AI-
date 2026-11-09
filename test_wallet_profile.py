import os
import tempfile
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import copy_trader as c
import wallet_profile as wp

W = str(Keypair().pubkey())
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
T0 = 1_760_000_000


def swap(mint, token_delta, sol_delta, t, program=PUMP, decimals=6):
    pre = 10**12
    raw = int(token_delta * 10**decimals)
    return {"blockTime": t, "meta": {"err": None, "preBalances": [10**11, 0], "postBalances": [10**11 + int(sol_delta * 1e9), 0],
            "preTokenBalances": [{"owner": W, "mint": mint, "uiTokenAmount": {"amount": str(pre), "decimals": decimals}}],
            "postTokenBalances": [{"owner": W, "mint": mint, "uiTokenAmount": {"amount": str(pre + raw), "decimals": decimals}}]},
            "transaction": {"message": {"accountKeys": [W, program]}}}


def history():
    """3 winners (+80% sells, ~20 min) and 2 losers (-25%), 1 SOL buys; one winner sold in two parts."""
    txs, t = [], T0
    mints = [str(Keypair().pubkey()) for _ in range(5)]
    for k, m in enumerate(mints):
        txs.append(swap(m, 1000, -1.0, t))
        if k < 3:
            if k == 0:
                txs.append(swap(m, -500, 0.9, t + 600))
                txs.append(swap(m, -500, 0.9, t + 1200))
            else:
                txs.append(swap(m, -1000, 1.8, t + 1200))
        else:
            txs.append(swap(m, -1000, 0.75, t + 900))
        t += 3600
    return txs, mints


class ParseTests(unittest.TestCase):
    def test_buy_and_sell(self):
        b = wp.parse_swap(swap("M", 100, -0.5, T0), W)
        self.assertEqual((b["side"], b["platform"]), ("buy", "pump.fun"))
        self.assertAlmostEqual(b["sol"], 0.5)
        s = wp.parse_swap(swap("M", -100, 0.6, T0), W)
        self.assertEqual(s["side"], "sell")
        self.assertIsNone(wp.parse_swap(swap("M", 0, -0.001, T0), W))

    def test_strategy_numbers(self):
        txs, _ = history()
        closed, open_now = wp.build_positions([wp.parse_swap(x, W) for x in txs], sol_usd=100)
        self.assertEqual(len(closed), 5)
        s = wp.summarize(closed, open_now)
        self.assertAlmostEqual(s["win_rate"], 0.6)
        self.assertAlmostEqual(s["tp_median"], 0.8, places=2)
        self.assertAlmostEqual(s["sl_median"], -0.25, places=2)
        self.assertAlmostEqual(s["size_median"], 100)
        self.assertAlmostEqual(s["scale_out"], 0.2)
        self.assertEqual(s["platforms"], {"pump.fun": 5})
        self.assertAlmostEqual(s["suggest_sl"], 0.25, places=2)
        text = wp.report(W, s)
        for piece in ("Take profit real", "Stop loss real", "COPY_TAKE_PROFIT = 0.80", "pump.fun 100%"):
            self.assertIn(piece, text)

    def test_analyze_reads_rpc_and_token_age(self):
        txs, mints = history()
        sigs = [{"signature": "s%d" % i, "err": None} for i in range(len(txs))][::-1]
        by_sig = {"s%d" % i: tx for i, tx in enumerate(txs)}

        def rpc(method, params):
            if method == "getSignaturesForAddress":
                return [] if "before" in params[1] else sigs
            if method == "getTokenSupply":
                return {"value": {"uiAmount": 1_000_000_000}}
            return by_sig[params[0]]

        def get_json(path):
            return [{"baseToken": {"address": m}, "pairCreatedAt": (T0 - 1800) * 1000} for m in mints]
        s, closed, text = wp.analyze(W, rpc, 100.0, get_json=get_json)
        self.assertEqual(s["positions"], 5)
        self.assertIsNotNone(s["token_age_median"])
        # 1 SOL (US$ 100) for 1000 tokens of a 1-billion supply coin = US$ 100 mi market value
        self.assertAlmostEqual(s["mcap_median"], 100_000_000)
        self.assertIn("Valor de mercado da moeda quando entra", text)
        self.assertIn("/estrategia", text)
        self.assertIn("ESTRATÉGIA", text)


class CloneTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_clone_sets_exit_and_seeds_holds(self):
        txs, _ = history()
        closed, open_now = wp.build_positions([wp.parse_swap(x, W) for x in txs], 100)
        s = wp.summarize(closed, open_now)
        ex = c.clone(W, s, closed)
        self.assertAlmostEqual(ex["tp"], 0.8, places=2)
        self.assertAlmostEqual(ex["sl"], 0.25, places=2)
        w = c._load()["wallets"][W]
        self.assertTrue(w["clone"])
        self.assertEqual(len(w["holds"]), 5)
        self.assertIn("CLONE: TP +80%", c.wallets_text())

    def test_sim_position_uses_clone_exit_and_time_stop(self):
        c.follow(W)
        d = c._load()
        d["wallets"][W].update(clone=True, exit={"tp": 0.3, "sl": 0.1, "max_hold_s": 60})
        d["positions"]["M"] = {"wallet": W, "real": False, "tokens": 1, "cost_usd": 10.0,
                               "opened_at": 0, "tp": 0.3, "sl": 0.1, "max_hold_s": 60}
        c._save(d)
        with patch.object(c, "_order", return_value={"outAmount": str(int(0.05 * 1e9))}), \
                patch.object(c, "_sol_usd", return_value=200.0), \
                patch.object(c, "_close", return_value=["fechou"]) as close:
            c.sim_exits()
        close.assert_called_with("M", "time_stop")      # price flat (US$ 10), held too long

    def test_real_exit_monitor_uses_position_tp_sl(self):
        import time
        import live_exit_monitor as m
        pos = {"mode": "copy", "copy_tp": 0.3, "copy_sl": 0.1, "opened_at": time.time()}
        self.assertEqual(m.position_signal(pos, 10, 13.1)["action"], "sell")     # +31% >= TP 30%
        self.assertEqual(m.position_signal(pos, 10, 11.0)["action"], "hold")
        self.assertEqual(m.position_signal(pos, 10, 8.9)["action"], "sell")      # -11% <= SL 10%
        old = dict(pos, copy_max_hold_s=60, opened_at=time.time() - 120)
        self.assertEqual(m.position_signal(old, 10, 10.5)["reason"], "time_stop")


if __name__ == "__main__":
    unittest.main()


class NewRulesTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_defaults_three_orders(self):
        import live_trade_state
        self.assertEqual((c.MAX_POSITIONS, c.MAX_OPEN_TOTAL, live_trade_state.MAX_POSITIONS), (3, 3, 3))

    def test_cloned_fast_wallet_never_opens(self):
        c.set_mode(True)
        c.follow(W)
        d = c._load()
        d["wallets"][W].update(clone=True, holds=[60, 60, 90], last_sig="s0")   # holds ~1 min
        c._save(d)
        buy = swap("MINTX", 1000, -1.0, T0)
        with patch.object(c, "new_signals", return_value=[("buy", "MINTX", 200.0)]), \
                patch.object(c, "_open") as op:
            c.cycle()
        op.assert_not_called()
        self.assertIn("não copia", c.status_text())

    def test_cloned_holder_opens(self):
        c.set_mode(True)
        c.follow(W)
        d = c._load()
        d["wallets"][W].update(clone=True, holds=[600, 700, 900])
        c._save(d)
        with patch.object(c, "new_signals", return_value=[("buy", "MINTX", 200.0)]), \
                patch.object(c, "_open", return_value=None) as op:
            c.cycle()
        op.assert_called_once()

    def test_total_cap_counts_copy_and_robot(self):
        import clone_strategy as cs
        d = cs._load()
        d["positions"] = {"A": {}, "B": {}}
        cs._save(d)
        d = c._load()
        d["positions"]["C"] = {"wallet": W, "real": False}
        c._save(d)
        self.assertEqual(c.open_sim_total(), 3)
        c.follow(W)
        with patch.object(c, "safety_reason", return_value=""), patch.object(c, "_sol_usd", return_value=200.0), \
                patch.object(c, "_order", return_value={"outAmount": "1000"}):
            self.assertIsNone(c._open("NEWMINT", W, None, None))
        self.assertNotIn("NEWMINT", c._load()["positions"])
