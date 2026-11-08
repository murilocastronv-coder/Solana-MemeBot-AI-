import base64
import time
import os
import tempfile
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import jito_arbitrage as j

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
import live_trade_state as _lts
AMOUNT = _lts.MEV_DEFAULTS["input_lamports"]
# required back = AMOUNT + tip + 10k base fees + max(10k, 0.1% of AMOUNT)
REQUIRED = AMOUNT + _lts.MEV_DEFAULTS["jito_tip_lamports"] + 10_000 + max(10_000, AMOUNT * 10 // 10_000)


def build(out, threshold):
    return {"outAmount": str(out), "otherAmountThreshold": str(threshold)}


class StrategyTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        patch.object(j, "_wallet", return_value=Keypair()).start()
        patch.object(j, "_mint_is_fixed", return_value=True).start()
        self.addCleanup(patch.stopall)

    def run_with(self, builds):
        calls = []

        def fake(*args, **kwargs):
            calls.append(kwargs.get("slippage_bps", 50))
            return builds.pop(0)
        with patch.object(j, "_build", side_effect=fake):
            return j.try_arbitrage(MINT, quote_only=True), calls

    def test_loss_is_skipped(self):
        result, _ = self.run_with([build(1000, 995), build(AMOUNT - 50_000, AMOUNT - 100_000)])
        self.assertEqual(result["status"], "no_edge")
        self.assertLess(result["estimated_net_lamports"], 0)

    def test_expected_profit_is_taken_with_locked_floor(self):
        # One sell quote only, with its minimum locked at the expected value.
        expected = REQUIRED + 30_000
        result, calls = self.run_with([build(1000, 995), build(expected, expected)])
        self.assertEqual(result["status"], "dry_run_opportunity")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], 0)
        self.assertGreaterEqual(result["estimated_net_lamports"], 10_000)

    def test_floor_never_below_required(self):
        self.assertEqual(j._locked_slippage_bps(100, 120), 0)
        bps = j._locked_slippage_bps(10_060_000, 10_030_000)
        self.assertGreaterEqual(10_060_000 * (10_000 - bps) // 10_000, 10_030_000)

    def test_duplicate_cu_limit_is_dropped(self):
        limit = {"programId": str(j.COMPUTE_BUDGET_PROGRAM), "accounts": [],
                 "data": base64.b64encode(b"\x02" + (400_000).to_bytes(4, "little")).decode()}
        price = {"programId": str(j.COMPUTE_BUDGET_PROGRAM), "accounts": [],
                 "data": base64.b64encode(b"\x03" + (1000).to_bytes(8, "little")).decode()}
        self.assertTrue(j._is_cu_limit(limit))
        self.assertFalse(j._is_cu_limit(price))

    def test_priority_fee_off_by_default(self):
        price = {"programId": str(j.COMPUTE_BUDGET_PROGRAM), "accounts": [],
                 "data": base64.b64encode(b"\x03" + (100_000).to_bytes(8, "little")).decode()}
        b = {"computeBudgetInstructions": [price]}
        os.environ.pop("MEV_PRIORITY_FEE_ENABLED", None)
        self.assertEqual(j._priority_fee_lamports(b), 0)
        with patch.dict(os.environ, {"MEV_PRIORITY_FEE_ENABLED": "true"}):
            self.assertEqual(j._priority_fee_lamports(b), 100_000)

    def test_live_failure_is_reported(self):
        expected = REQUIRED + 30_000
        builds = [build(1000, 995), build(expected, expected)]
        with patch.object(j, "_build", side_effect=lambda *a, **k: builds.pop(0)), \
             patch.object(j, "_live_enabled", return_value=True), \
             patch.object(j, "_preflight", side_effect=RuntimeError("Bot wallet SOL balance is below the trade")):
            with self.assertRaises(j.LiveSendError) as ctx:
                j.try_arbitrage(MINT)
        self.assertEqual(ctx.exception.reason, "saldo SOL insuficiente")
        self.assertGreater(ctx.exception.net, 0)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        j._LAST_CHECKED.clear()
        patch.object(j, "_labels_for_dex_id", side_effect=lambda d: [d]).start()
        self.addCleanup(patch.stopall)

    def item(self, token, liq=50_000, prices=(1.0, 1.0), status="REJECT", reasons=("onchain_security_not_verified",)):
        pools = [{"dex_id": "dex" + str(i), "price_usd": p, "liquidity_usd": liq} for i, p in enumerate(prices)]
        return {"token": token, "liquidity_usd": liq, "dex_pools": pools, "status": status, "reasons": list(reasons)}

    def test_all_tokens_considered_and_spread_first(self):
        rows = [self.item("A", prices=(1.0, 1.001)), self.item("B", prices=(1.0, 1.02)),
                self.item("young", prices=(1.0, 1.005), reasons=("pair_younger_than_24h", "x")),
                self.item("thin", liq=500), self.item("bad", reasons=("not_solana",))]
        picked = [i["token"] for i, _ in j.pick_candidates(rows, limit=10, now=0)]
        self.assertEqual(picked, ["B", "young", "A"])

    def test_big_spread_token_waits_its_turn(self):
        rows = [self.item("BIG", prices=(1.0, 1.05)), self.item("A"), self.item("B")]
        first = [i["token"] for i, _ in j.pick_candidates(rows, limit=1, now=1000)]
        self.assertEqual(first, ["BIG"])
        j._LAST_CHECKED["BIG"] = 1000
        second = [i["token"] for i, _ in j.pick_candidates(rows, limit=1, now=1010)]
        self.assertNotEqual(second, ["BIG"])
        self.assertIn("Moedas na lista: 3", j.coverage_text(now=1010))

    def test_cooldown_skips_phantom_token(self):
        j._COOLDOWN["B"] = time.time() + 600
        rows = [self.item("A"), self.item("B")]
        self.assertEqual([i["token"] for i, _ in j.pick_candidates(rows, now=5_000)], ["A"])
        j._COOLDOWN["B"] = time.time() - 1
        self.assertEqual(len(j.pick_candidates(rows, now=20_000)), 2)
        j._COOLDOWN.clear()

    def test_phantom_pause_grows_and_survives_restart(self):
        import tempfile, os
        from pathlib import Path
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"MEV_PENDING_FILE": str(Path(d) / "p.json")}):
            j._COOLDOWN.clear(); j._FAILS.clear(); j._COOLDOWN_LOADED[0] = True
            j._pause_phantom("X")
            first = j._COOLDOWN["X"] - time.time()
            j._pause_phantom("X")
            second = j._COOLDOWN["X"] - time.time()
            self.assertAlmostEqual(first, 1800, delta=5)
            self.assertAlmostEqual(second, 3600, delta=5)
            j._COOLDOWN.clear(); j._FAILS.clear(); j._COOLDOWN_LOADED[0] = False   # restart
            self.assertTrue(j._cooling("X"))
            self.assertEqual(j._FAILS["X"], 2)
        j._COOLDOWN.clear(); j._FAILS.clear(); j._COOLDOWN_LOADED[0] = False

    def test_rotation_skips_recently_checked(self):
        rows = [self.item(str(n)) for n in range(25)]
        first = {i["token"] for i, _ in j.pick_candidates(rows, limit=10, now=1000)}
        for t in first:
            j._LAST_CHECKED[t] = 1000
        second = {i["token"] for i, _ in j.pick_candidates(rows, limit=10, now=1010)}
        self.assertFalse(first & second)
        self.assertEqual(len(second), 10)

class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.tx = [b"one", b"two"]

    def test_falls_back_to_buy_leg_when_rpc_lacks_simulate_bundle(self):
        calls = []

        def rpc(method, params):
            calls.append(method)
            if method == "simulateBundle":
                return {"error": {"code": -32601, "message": "Method not found"}}
            return {"result": {"value": {"err": None}}}
        with patch.object(j, "_rpc", side_effect=rpc):
            self.assertEqual(len(j._simulate_bundle(self.tx)), 2)
        self.assertEqual(calls, ["simulateBundle", "simulateTransaction"])

    def test_buy_leg_failure_blocks(self):
        def rpc(method, params):
            if method == "simulateBundle":
                raise RuntimeError("RPC simulateBundle HTTP 400")
            return {"result": {"value": {"err": {"InstructionError": [0, "x"]}}}}
        with patch.object(j, "_rpc", side_effect=rpc):
            with self.assertRaises(RuntimeError):
                j._simulate_bundle(self.tx)

    def test_bundle_simulation_success(self):
        with patch.object(j, "_rpc", return_value={"result": {"value": {"summary": "succeeded", "transactionResults": []}}}):
            self.assertEqual(len(j._simulate_bundle(self.tx)), 2)

    def test_jito_error_names_step(self):
        self.assertEqual(j._error_code(RuntimeError("Jito sendBundle HTTP 400: bundle invalid")),
                         "Jito sendBundle HTTP 400: bundle invalid")


class SkipCooldownTests(unittest.TestCase):
    def setUp(self):
        j._COOLDOWN.clear(); j._LAST_CHECKED.clear(); j._VISITS.clear()
        self.addCleanup(j._COOLDOWN.clear)

    def _cycle(self, side_effect):
        item = {"token": MINT, "liquidity_usd": 50_000, "dex_pools": [], "reasons": []}
        with patch.object(j, "try_arbitrage", side_effect=side_effect), \
             patch.object(j, "_live_enabled", return_value=False), \
             patch("live_trade_state.record_mev_event"):
            return j.scan_cycle([item], force=True, quote_only=True)

    def test_authority_token_paused_long(self):
        self._cycle(RuntimeError("Mint or freeze authority remains active"))
        self.assertGreater(j._COOLDOWN[MINT] - time.monotonic(), 3600)

    def test_no_route_falls_back_to_normal_route(self):
        import requests
        resp = requests.Response(); resp.status_code = 400
        calls = []

        def fake(mint, quote_only=False, buy_dexes=None, sell_dexes=None):
            calls.append(buy_dexes)
            if buy_dexes:
                raise requests.HTTPError(response=resp)
            return {"status": "no_edge", "mint": mint, "estimated_net_lamports": -1}
        route = {"buy_dexes": ["A"], "sell_dexes": ["B"], "spread_pct": 2.0, "buy_dex_id": "a", "sell_dex_id": "b"}
        with patch.object(j, "pick_candidates", return_value=[({"token": MINT}, route)]):
            out = self._cycle(fake)
        self.assertEqual(calls, [["A"], None])
        self.assertEqual(out[0]["status"], "no_edge")


class CacheTests(unittest.TestCase):
    def test_tip_accounts_and_balance_are_cached(self):
        from solders.keypair import Keypair
        j._TIP_CACHE.update(at=0.0, accounts=[]); j._BALANCE_CACHE.update(at=0.0, owner="", lamports=-1)
        tip = str(Keypair().pubkey())
        with patch.object(j, "_jito", return_value=[tip]) as jito, \
             patch.object(j, "_rpc", return_value={"result": {"value": 123}}) as rpc:
            for _ in range(3):
                self.assertEqual(str(j._tip_account()), tip)
                self.assertEqual(j._cached_balance("W"), 123)
        self.assertEqual((jito.call_count, rpc.call_count), (1, 1))


class TriangularTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}).start()
        patch.object(j, "_wallet", return_value=Keypair()).start()
        patch.object(j, "_mint_is_fixed", return_value=True).start()
        self.addCleanup(patch.stopall)

    def run_tri(self, builds, usdc_first=True, **kw):
        calls = []

        def fake(inp, out, amount, taker, dexes=None, slippage_bps=50):
            calls.append((inp, out, amount, slippage_bps))
            return builds.pop(0)
        with patch.object(j, "_build", side_effect=fake):
            return j.try_triangular(MINT, usdc_first=usdc_first, quote_only=kw.get("quote_only", True)), calls

    def test_path_and_minimum_chaining(self):
        from jupiter_v2_executor import USDC_MINT
        result, calls = self.run_tri([build(9_000_000, 8_980_000), build(5_000, 4_990), build(AMOUNT, AMOUNT)])
        self.assertEqual([(c[0], c[1]) for c in calls], [(j.SOL_MINT, USDC_MINT), (USDC_MINT, MINT), (MINT, j.SOL_MINT)])
        self.assertEqual(calls[1][2], 8_980_000)      # leg 2 sells leg 1's guaranteed minimum
        self.assertEqual(calls[2][2], 4_990)
        self.assertEqual(calls[2][3], 0)              # last leg locked at expected value
        self.assertEqual(result["status"], "no_edge")  # came back with what went in: costs make it negative
        self.assertEqual(result["route"], "SOL→USDC→token→SOL")

    def test_profitable_triangle_and_reverse_direction(self):
        back = AMOUNT + 200_000
        result, calls = self.run_tri([build(5_000, 4_990), build(9_000_000, 8_980_000), build(back, back)], usdc_first=False)
        self.assertEqual(result["status"], "dry_run_opportunity")
        self.assertEqual(result["route"], "SOL→token→USDC→SOL")
        self.assertEqual(calls[0][1], MINT)

    def test_live_send_bundles_three_legs(self):
        back = AMOUNT + 200_000
        sent = {}
        with patch.object(j, "_live_enabled", return_value=True), \
             patch.object(j, "_send_live", side_effect=lambda m, s, a, t, builds, n: sent.update(n=len(builds)) or
                          {"status": "submitted_pending", "bundle_id": "B", "mint": m, "estimated_net_lamports": n}):
            result, _ = self.run_tri([build(9_000_000, 8_980_000), build(5_000, 4_990), build(back, back)], quote_only=False)
        self.assertEqual(sent["n"], 3)
        self.assertEqual(result["status"], "submitted_pending")
        self.assertEqual(result["route"], "SOL→USDC→token→SOL")


class ErrorLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(j._error_code(RuntimeError("Bot wallet SOL balance is below the trade and fee reserve")),
                         "saldo SOL insuficiente")
        self.assertEqual(j._error_code(ValueError("x")), "ValueError")


if __name__ == "__main__":
    unittest.main()
