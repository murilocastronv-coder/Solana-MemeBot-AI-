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
            rows = self.txs.get(pair, [])
            if "until" in opts:
                return []
            return [{"signature": sig, "err": None} for sig, _ in rows]
        if method == "getTransaction":
            for rows in self.txs.values():
                for sig, tx in rows:
                    if sig == params[0]:
                        return tx
        return None


class DexWalletTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

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
        s = wallet_copy.summary()
        self.assertEqual(s["dex_transactions_scanned"], 6)
        self.assertEqual(len(s["dex_tokens"]), 3)

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
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

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

    def test_fast_flipper_history_is_rejected(self):
        dw.scan(get=fake_dex(), rpc=self.rpc(hold=40), now=1_800_000_000)
        self.assertEqual(wallet_copy.best_wallets(), [])


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
