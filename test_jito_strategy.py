import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import jito_arbitrage as j

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
AMOUNT = 10_000_000


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
        # +2% expected: old worst-case math (-0.5%) would still pass, but +0.6%
        # expected would not. Use +0.6% to show the new behaviour.
        expected = AMOUNT + 60_000
        floor_build = build(expected, AMOUNT + 25_000)
        result, calls = self.run_with([build(1000, 995), build(expected, expected - 50_000), floor_build])
        self.assertEqual(result["status"], "dry_run_opportunity")
        self.assertEqual(len(calls), 3)
        self.assertLess(calls[-1], 50)
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


if __name__ == "__main__":
    unittest.main()
