import os
import tempfile
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import dex_wallets as dw
import wallet_copy

TRADER = str(Keypair().pubkey())
MINTS = [str(Keypair().pubkey()) for _ in range(3)]
PAIRS = {m: str(Keypair().pubkey()) for m in MINTS}


def fake_dex(liq=50000, vol=200000):
    def get(path):
        if path.startswith("/token-boosts/top"):
            return [{"chainId": "solana", "tokenAddress": m} for m in MINTS] + [{"chainId": "ethereum", "tokenAddress": "0x1"}]
        if path.startswith("/tokens/v1/solana/"):
            return [{"chainId": "solana", "pairAddress": PAIRS[m], "baseToken": {"address": m, "symbol": "M%d" % i},
                     "liquidity": {"usd": liq}, "volume": {"h24": vol}} for i, m in enumerate(MINTS)]
        return []
    return get


def swap_tx(mint, token_delta, sol_delta, t):
    pre, post = 1_000_000, 1_000_000 + token_delta
    return {"blockTime": t, "slot": t, "meta": {"err": None, "preBalances": [10**10], "postBalances": [10**10 + sol_delta],
            "preTokenBalances": [{"owner": TRADER, "mint": mint, "uiTokenAmount": {"amount": str(pre), "decimals": 6}}],
            "postTokenBalances": [{"owner": TRADER, "mint": mint, "uiTokenAmount": {"amount": str(post), "decimals": 6}}]},
            "transaction": {"message": {"accountKeys": [TRADER]}, "signatures": ["s"]}}


class FakeRpc:
    """Each pair: the trader buys, then sells 10 minutes later with profit."""
    def __init__(self):
        self.calls, self.txs = [], {}
        for k, m in enumerate(MINTS):
            t0 = 1_700_000_000 + k * 1000
            self.txs[PAIRS[m]] = [("sell-" + m, swap_tx(m, -500_000, 2 * 10**8, t0 + 600)),   # newest first
                                  ("buy-" + m, swap_tx(m, 500_000, -10**8, t0))]

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "getSignaturesForAddress":
            pair, opts = params
            if pair == TRADER:   # the trader's own history = every trade it made, newest first
                rows = sorted((r for rs in self.txs.values() for r in rs), key=lambda r: -r[1]["blockTime"])
                if "before" in opts:
                    return []
            else:
                rows = self.txs.get(pair, [])
            if "until" in opts:
                return []
            return [{"signature": sig, "err": None, "blockTime": tx["blockTime"]} for sig, tx in rows]
        if method == "getTransaction":
            for rows in self.txs.values():
                for sig, tx in rows:
                    if sig == params[0]:
                        return tx
        return None


class DexWalletTests(unittest.TestCase):
    def setUp(self):
        import copy_trader
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        for name in ("old_enough", "active_daily", "has_min_balance"):
            q = patch.object(copy_trader, name, return_value=True)
            q.start()
            self.addCleanup(q.stop)

    def test_trending_filters_chain_and_liquidity(self):
        self.assertEqual(len(dw.trending_pairs(fake_dex())), 3)
        self.assertEqual(dw.trending_pairs(fake_dex(liq=1000)), [])
        self.assertEqual(dw.trending_pairs(fake_dex(vol=10)), [])

    def test_profitable_holder_on_hot_coins_is_found(self):
        rpc = FakeRpc()
        n = dw.scan(get=fake_dex(), rpc=rpc)
        self.assertEqual(n, 6)
        best = wallet_copy.best_wallets()
        self.assertEqual(best[0]["wallet"], TRADER)
        self.assertGreaterEqual(best[0]["average_hold_seconds"], 300)
        self.assertEqual(best[0]["average_buy_lamports"], 10**8)
        s = wallet_copy.summary()
        self.assertEqual(s["dex_transactions_scanned"], 6)
        self.assertEqual(len(s["dex_tokens"]), 3)

    def test_reads_new_transaction_version(self):
        rpc = FakeRpc()
        dw.scan(get=fake_dex(), rpc=rpc)
        opts = [p[1] for m, p in rpc.calls if m == "getTransaction"]
        self.assertTrue(opts and all(o["maxSupportedTransactionVersion"] == 1 for o in opts))

    def test_second_scan_only_reads_new_trades(self):
        rpc = FakeRpc()
        dw.scan(get=fake_dex(), rpc=rpc)
        rpc.calls.clear()
        self.assertEqual(dw.scan(get=fake_dex(), rpc=rpc), 0)
        sig_calls = [p for m, p in rpc.calls if m == "getSignaturesForAddress"]
        self.assertTrue(all("until" in opts for _, opts in sig_calls))

    def test_quick_flipper_is_not_approved(self):
        rpc = FakeRpc()
        for m in MINTS:   # sells after 60 s
            sell = rpc.txs[PAIRS[m]][0][1]
            sell["blockTime"] = rpc.txs[PAIRS[m]][1][1]["blockTime"] + 60
        dw.scan(get=fake_dex(), rpc=rpc)
        self.assertEqual(wallet_copy.best_wallets(), [])


if __name__ == "__main__":
    unittest.main()


class WalletHistoryTests(unittest.TestCase):
    """The pair scan only catches the BUY; the wallet's own history proves it holds and profits."""

    def setUp(self):
        import copy_trader
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        self.screen = {"old": True, "active": True, "rich": True}
        for name, key in (("old_enough", "old"), ("active_daily", "active"), ("has_min_balance", "rich")):
            q = patch.object(copy_trader, name, side_effect=lambda *_a, k=key: self.screen[k])
            q.start()
            self.addCleanup(q.stop)

    def rpc(self, hold=600):
        txs = {}
        pair_rows = {}
        history = []
        for k, m in enumerate(MINTS):
            t0 = 1_700_000_000 + k * 5000
            txs["hb-" + m] = swap_tx(m, 500_000, -10**8, t0)
            txs["hs-" + m] = swap_tx(m, -500_000, 2 * 10**8, t0 + hold)
            history = [("hs-" + m), ("hb-" + m)] + history          # newest first
            txs["pb-" + m] = swap_tx(m, 500_000, -10**8, t0 + 9000)  # fresh buy on the hot pair
            pair_rows[PAIRS[m]] = ["pb-" + m]
        calls = []

        def rpc(method, params):
            calls.append((method, params[0]))
            if method == "getSignaturesForAddress":
                addr, opts = params
                if "until" in opts:
                    return []
                rows = history if addr == TRADER else pair_rows.get(addr, [])
                return [{"signature": s, "err": None} for s in rows[:opts["limit"]]]
            return txs.get(params[0])
        rpc.calls = calls
        return rpc

    def test_holder_approved_from_history_in_one_cycle(self):
        rpc = self.rpc()
        dw.scan(get=fake_dex(), rpc=rpc, now=1_800_000_000)
        best = wallet_copy.best_wallets()
        self.assertEqual(best[0]["wallet"], TRADER)
        self.assertEqual(best[0]["closed_trades"], 3)
        self.assertEqual(wallet_copy.summary()["wallets_analyzed"], 1)

    def test_history_not_reread_within_12h(self):
        rpc = self.rpc()
        dw.scan(get=fake_dex(), rpc=rpc, now=1_800_000_000)
        rpc.calls.clear()
        dw.scan(get=fake_dex(), rpc=rpc, now=1_800_000_000 + 600)
        self.assertNotIn(("getSignaturesForAddress", TRADER), rpc.calls)

    def test_screening_rejects_before_reading_history(self):
        for key, reason in (("old", "idade"), ("active", "atividade"), ("rich", "saldo")):
            d = tempfile.mkdtemp()
            with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}):
                self.screen = {"old": True, "active": True, "rich": True}
                self.screen[key] = False
                rpc = self.rpc()
                dw.scan(get=fake_dex(), rpc=rpc, now=1_800_000_000)
                history_reads = [p for m, p in rpc.calls if m == "getTransaction" and p.startswith("h")]
                self.assertEqual(history_reads, [])
                c = wallet_copy.summary()["last_cycle"]
                self.assertEqual(c["rejected"][reason], 1)
                self.assertEqual(c["histories"], 0)
                self.assertEqual(wallet_copy.best_wallets(), [])

    def test_fast_flipper_history_is_rejected(self):
        dw.scan(get=fake_dex(), rpc=self.rpc(hold=40), now=1_800_000_000)
        self.assertEqual(wallet_copy.best_wallets(), [])


class WalletAgeTests(unittest.TestCase):
    NOW = 1_800_000_000

    def pages(self, times):
        """Fake RPC with signatures at the given times (newest first), 1000 per page."""
        rows = [{"signature": "s%d" % i, "blockTime": t} for i, t in enumerate(times)]
        calls = []

        def rpc(method, params):
            calls.append(params[1])
            opts = params[1]
            start = 0
            if "before" in opts:
                start = next(i for i, r in enumerate(rows) if r["signature"] == opts["before"]) + 1
            return rows[start:start + opts["limit"]]
        rpc.calls = calls
        return rows, rpc

    def test_old_wallet_proven_from_first_page(self):
        rows, rpc = self.pages([self.NOW - d * 86400 for d in (1, 5, 60)])
        self.assertGreaterEqual(dw.wallet_age_days("W", rpc, self.NOW, first_page=rows), 7)
        self.assertEqual(rpc.calls, [])               # no extra RPC call needed

    def test_young_wallet(self):
        rows, rpc = self.pages([self.NOW - d * 86400 for d in (1, 3, 5)])
        self.assertLess(dw.wallet_age_days("W", rpc, self.NOW, first_page=rows[:2]), 7)

    def test_pages_back_for_busy_wallet(self):
        times = [self.NOW - i * 3000 for i in range(2500)]   # ~87 days over 2500 txs
        rows, rpc = self.pages(times)
        age = dw.wallet_age_days("W", rpc, self.NOW, first_page=rows[:50])
        self.assertGreaterEqual(age, 7)
        self.assertLessEqual(len(rpc.calls), 1)

    def test_default_minimum_is_seven_days(self):
        import copy_trader
        self.assertEqual(dw.MIN_AGE_DAYS, 7)
        self.assertEqual(copy_trader.MIN_WALLET_DAYS, 7)
        self.assertFalse(copy_trader.old_enough({"age_days": 5}))
        self.assertTrue(copy_trader.old_enough({"age_days": 8}))

    def test_counts_active_days_in_last_week(self):
        times = [self.NOW - d * 86400 - 3600 for d in (0, 1, 2, 4, 6, 6, 40)]   # days 0,1,2,4,6 + old
        rows, rpc = self.pages(times)
        act = dw.wallet_activity("W", rpc, self.NOW, first_page=rows)
        self.assertEqual(act["active_days_7"], 5)
        self.assertGreaterEqual(act["age_days"], 40)

    def test_failed_transactions_do_not_count_as_activity(self):
        rows, rpc = self.pages([self.NOW - 100, self.NOW - 2 * 86400, self.NOW - 50 * 86400])
        rows[0]["err"] = {"x": 1}
        self.assertEqual(dw.wallet_activity("W", rpc, self.NOW, first_page=rows)["active_days_7"], 1)

    def test_too_busy_to_prove_is_unknown(self):
        times = [self.NOW - i * 60 for i in range(5000)]     # 5000 txs in 3.5 days
        rows, rpc = self.pages(times)
        self.assertIsNone(dw.wallet_age_days("W", rpc, self.NOW, first_page=rows[:50], pages=2))


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.posts = list(responses), 0

    def post(self, *a, **k):
        self.posts += 1
        return self.responses.pop(0)


class RpcLimiterTests(unittest.TestCase):
    def make(self, responses):
        self.sleeps, self.t = [], [0.0]

        def sleep(x):
            self.sleeps.append(x)
            self.t[0] += x
        self.session = FakeSession(responses)
        return dw._rpc_factory("http://rpc", sleep=sleep, clock=lambda: self.t[0], session=self.session)

    def test_retries_rate_limit_then_succeeds(self):
        rpc = self.make([FakeResp(429), FakeResp(200, {"error": {"message": "Too many requests"}}),
                         FakeResp(200, {"result": 7})])
        before = dw.RPC_ERRORS["count"]
        self.assertEqual(rpc("getSlot", []), 7)
        self.assertEqual(self.session.posts, 3)
        self.assertEqual(dw.RPC_ERRORS["count"] - before, 2)

    def test_real_error_is_not_retried(self):
        rpc = self.make([FakeResp(200, {"error": {"message": "invalid param"}})])
        with self.assertRaises(ValueError):
            rpc("getTransaction", ["x"])
        self.assertEqual(self.session.posts, 1)

    def test_throttles_to_max_rps(self):
        rpc = self.make([FakeResp(200, {"result": 1}) for _ in range(5)])
        for _ in range(5):
            rpc("getSlot", [])
        self.assertGreaterEqual(self.t[0], 4 / dw.MAX_RPS - 1e-9)

    def test_cycle_report_saved(self):
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}):
            dw.scan(get=fake_dex(), rpc=FakeRpc(), now=1_800_000_000)
            c = wallet_copy.summary()["last_cycle"]
        self.assertEqual(c["tx_read"], 6)
        self.assertEqual(c["failed"], 0)
        self.assertGreaterEqual(c["buyers"], 1)


class StaleApprovalTests(unittest.TestCase):
    """Wallets approved before the age / activity / buy-size filters existed."""

    def setUp(self):
        import copy_trader
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        self.ok = True
        for name in ("old_enough", "active_daily", "has_min_balance"):
            q = patch.object(copy_trader, name, side_effect=lambda *_a: self.ok)
            q.start()
            self.addCleanup(q.stop)
        state = wallet_copy.empty_state()
        state["wallets"][TRADER] = {"closed_trades": 5, "wins": 5, "realized_lamports": 10**8,
                                    "total_hold_seconds": 5 * 900, "long_holds": 5, "hold_samples": [900] * 5}
        state["dex_analyzed"] = {TRADER: 1_800_000_000 - 60}     # analysed 1 min ago (old code)
        wallet_copy._save_state(wallet_copy._state_path(), state)

    def test_unmeasured_buy_size_is_none_not_zero(self):
        self.assertIsNone(wallet_copy.best_wallets()[0]["average_buy_lamports"])

    def test_old_approval_is_reanalysed_now(self):
        rpc = WalletHistoryTests.rpc(self)
        dw.scan(get=lambda p: [], rpc=rpc, now=1_800_000_000)
        st = wallet_copy._load_state(wallet_copy._state_path())["wallets"][TRADER]
        self.assertIn("cost_lamports_closed", st)
        self.assertIn(("getSignaturesForAddress", TRADER), rpc.calls)

    def test_old_approval_failing_filters_is_dropped(self):
        self.ok = False
        dw.scan(get=lambda p: [], rpc=WalletHistoryTests.rpc(self), now=1_800_000_000)
        self.assertNotIn(TRADER, wallet_copy._load_state(wallet_copy._state_path())["wallets"])
